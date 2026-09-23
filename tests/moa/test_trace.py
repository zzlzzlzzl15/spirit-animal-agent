"""MoA 完整轮次追踪持久化测试 — 对标 Hermes ``tests/run_agent/test_moa_loop_mode.py``
的 ``test_moa_full_trace_written_when_enabled`` / ``test_moa_trace_not_written_when_disabled``。

启用 ``moa.save_traces`` 后，每个真正跑了参考 fan-out 的 MoA 轮（缓存 MISS）向
``<spirit_home>/moa-traces/<session_id>.jsonl`` 追加一行 JSON，记录每个参考收到的
**完整**输入 messages + 输出，以及聚合器收到的**完整**输入（含注入的参考 guidance）+
输出——一次运行可离线端到端审计。

这些测试走**真实配置路径**（``moa_home`` fixture 把 ``SPIRIT_HOME`` 隔离到 tmp_path +
写真实 ``config.yaml``），因为 ``moa_trace._traces_enabled_and_dir()`` 读真实
``load_config()``（无缓存）；只 monkeypatch transport seam（``_call_slot`` /
``_call_slot_stream``）避免碰网络。

与 Hermes 的关键差异（Spirit 追踪形状）：
- usage 键是 ``prompt_tokens``/``completion_tokens``/``total_tokens``（**不是** Hermes 的
  ``input_tokens``/``output_tokens``）。
- **无** ``cost_usd`` 字段（Spirit 无定价表）。
- 聚合器记录含 ``output_location``（``inline`` / ``inline_from_stream`` /
  ``assistant_message_in_session_db``），标识输出从何而来。
"""

from __future__ import annotations

import json

from spirit.moa import moa_loop
from spirit.moa.moa_loop import MoAChatCompletions

from .conftest import content_event, done_event, fake_response, write_moa_yaml


# ---------------------------------------------------------------------------
# 辅助：patch transport seam（避免网络），保留真实配置路径
# ---------------------------------------------------------------------------

def _patch_transport(monkeypatch, *, ref_prompt=500, ref_completion=80, agg_content="AGGREGATOR FINAL ANSWER"):
    """把 ``_call_slot`` 替换为假件：参考回显模型名 + 固定用量，聚合器返回固定文本。"""
    def fake_call_slot(slot, messages, *, task="moa_reference", temperature=None, max_tokens=None, tools=None):
        if task == "moa_reference":
            return fake_response(
                f"advice from {slot.get('model')}",
                prompt=ref_prompt, completion=ref_completion,
            )
        return fake_response(agg_content)

    monkeypatch.setattr(moa_loop, "_call_slot", fake_call_slot)


_REVIEW_PRESET_YAML = """
moa:
  save_traces: true
  default_preset: review
  presets:
    review:
      reference_models:
        - provider: openrouter
          model: adv-a
        - provider: openrouter
          model: adv-b
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
"""


def _read_records(trace_file):
    return [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# 启用时写完整轮次
# ---------------------------------------------------------------------------

def test_full_trace_written_when_enabled(moa_home, monkeypatch):
    """save_traces 开启时，一条完整 MoA 轮被写入 JSONL。

    断言记录捕获每个参考的**完整**输入 messages + 输出，以及聚合器的**完整**输入
    （含注入的参考 guidance）+ 输出——真正的完整轮次，可离线审计。
    """
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    # 非流式 create() → 聚合器输出内联捕获。
    facade.create(messages=[{"role": "user", "content": "please review the plan"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-xyz")

    trace_file = moa_home / "moa-traces" / "sess-xyz.jsonl"
    assert trace_file.exists(), "追踪文件未写入"
    records = _read_records(trace_file)
    assert len(records) == 1
    rec = records[0]

    # 轮次框架。
    assert rec["session_id"] == "sess-xyz"
    assert rec["preset"] == "review"
    assert isinstance(rec["ts"], (int, float))

    # 两个参考都被捕获，各含完整输入 messages + 输出。
    assert len(rec["references"]) == 2
    for ref in rec["references"]:
        assert ref["model"] in ("adv-a", "adv-b")
        assert ref["provider"] == "openrouter"
        assert ref["label"] == f"openrouter:{ref['model']}"
        # 完整输入 messages 存在（advisory 系统提示 + advisory 视图）。
        assert isinstance(ref["input_messages"], list) and len(ref["input_messages"]) >= 2
        assert ref["input_messages"][0]["role"] == "system"
        # 完整输出存在且模型特定。
        assert ref["output"] == f"advice from {ref['model']}"
        # Spirit usage 形状：prompt_tokens（不是 Hermes 的 input_tokens）。
        assert ref["usage"]["prompt_tokens"] == 500
        assert ref["usage"]["completion_tokens"] == 80
        assert ref["usage"]["total_tokens"] == 580
        # Spirit 无定价表 → 无 cost_usd 字段。
        assert "cost_usd" not in ref
        assert "input_tokens" not in ref["usage"]

    # 聚合器：完整输入（含注入的参考 guidance）+ 内联输出。
    agg = rec["aggregator"]
    assert agg["model"] == "anthropic/claude-opus-4.8"
    assert agg["provider"] == "openrouter"
    assert agg["streamed"] is False
    assert agg["output"] == "AGGREGATOR FINAL ANSWER"
    assert agg["output_location"] == "inline"
    agg_text = json.dumps(agg["input_messages"], ensure_ascii=False)
    assert "Mixture of Agents reference context" in agg_text
    assert "advice from adv-a" in agg_text and "advice from adv-b" in agg_text


def test_trace_not_written_when_disabled(moa_home, monkeypatch):
    """默认（save_traces 关）什么都不写。"""
    write_moa_yaml(
        moa_home,
        """
moa:
  default_preset: review
  presets:
    review:
      reference_models:
        - provider: openrouter
          model: adv-a
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""",
    )
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-off")

    assert not (moa_home / "moa-traces").exists()


def test_trace_dir_override_respected(moa_home, monkeypatch):
    """``moa.trace_dir`` 覆盖默认的 ``<spirit_home>/moa-traces/``。"""
    custom = moa_home / "custom-traces"
    write_moa_yaml(
        moa_home,
        f"""
moa:
  save_traces: true
  trace_dir: "{custom.as_posix()}"
  default_preset: review
  presets:
    review:
      reference_models:
        - provider: openrouter
          model: adv-a
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""",
    )
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-custom")

    assert (custom / "sess-custom.jsonl").exists()
    # 默认目录未被使用。
    assert not (moa_home / "moa-traces").exists()


# ---------------------------------------------------------------------------
# 流式轮次的 output_location
# ---------------------------------------------------------------------------

def test_stream_trace_uses_fallback_output(moa_home, monkeypatch):
    """流式轮 + consume_and_save_trace(fallback) → output_location=inline_from_stream。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    def fake_stream(slot, messages, *, task="moa_aggregator", temperature=None, max_tokens=None, tools=None):
        return iter([content_event("streamed "), content_event("answer"), done_event(1, 2)])

    monkeypatch.setattr(moa_loop, "_call_slot_stream", fake_stream)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)
    # 调用方事后把解析出的 assistant 文本折入。
    facade.consume_and_save_trace(session_id="sess-stream", aggregator_output_fallback="streamed answer")

    rec = _read_records(moa_home / "moa-traces" / "sess-stream.jsonl")[0]
    agg = rec["aggregator"]
    assert agg["streamed"] is True
    assert agg["output"] == "streamed answer"
    assert agg["output_location"] == "inline_from_stream"


def test_stream_trace_without_fallback_points_to_session_db(moa_home, monkeypatch):
    """流式轮无 fallback 输出 → output_location=assistant_message_in_session_db，output=None。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    def fake_stream(slot, messages, *, task="moa_aggregator", temperature=None, max_tokens=None, tools=None):
        return iter([content_event("x"), done_event(0, 0)])

    monkeypatch.setattr(moa_loop, "_call_slot_stream", fake_stream)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)
    facade.consume_and_save_trace(session_id="sess-nofb")

    rec = _read_records(moa_home / "moa-traces" / "sess-nofb.jsonl")[0]
    agg = rec["aggregator"]
    assert agg["streamed"] is True
    assert agg["output"] is None
    assert agg["output_location"] == "assistant_message_in_session_db"


# ---------------------------------------------------------------------------
# 文件名安全 / 多轮追加 / 缓存 HIT 不写
# ---------------------------------------------------------------------------

def test_session_id_sanitized_into_filename(moa_home, monkeypatch):
    """session id 里的不安全字符被替换，产出合法文件名。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    facade.consume_and_save_trace(session_id="sess/../weird:id*")

    # 斜杠/冒号/星号被替换成下划线，落在 moa-traces 目录内（未逃逸）。
    files = list((moa_home / "moa-traces").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "sess_.._weird_id_.jsonl"


def test_multiple_turns_append_to_same_file(moa_home, monkeypatch):
    """两个缓存 MISS 轮（不同 user 消息）向同一会话文件追加两行。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-multi")
    facade.create(messages=[{"role": "user", "content": "turn two"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-multi")

    records = _read_records(moa_home / "moa-traces" / "sess-multi.jsonl")
    assert len(records) == 2


def test_cache_hit_does_not_write_trace(moa_home, monkeypatch):
    """缓存 HIT 的冗余 create()（相同状态）不写第二条追踪——完整轮次只在 MISS 上追踪。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    msgs = [{"role": "user", "content": "same state"}]
    facade.create(messages=msgs, tools=[])
    facade.consume_and_save_trace(session_id="sess-hit")
    # 相同状态的冗余调用 → 缓存 HIT，无新追踪。
    facade.create(messages=msgs, tools=[])
    facade.consume_and_save_trace(session_id="sess-hit")

    records = _read_records(moa_home / "moa-traces" / "sess-hit.jsonl")
    assert len(records) == 1


def test_consume_trace_is_idempotent(moa_home, monkeypatch):
    """重复 consume_and_save_trace 不双写——待处理追踪在首次 consume 后清空。"""
    write_moa_yaml(moa_home, _REVIEW_PRESET_YAML)
    _patch_transport(monkeypatch)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    facade.consume_and_save_trace(session_id="sess-idem")
    # 第二次 consume（无新 create()）→ no-op。
    facade.consume_and_save_trace(session_id="sess-idem")

    records = _read_records(moa_home / "moa-traces" / "sess-idem.jsonl")
    assert len(records) == 1
