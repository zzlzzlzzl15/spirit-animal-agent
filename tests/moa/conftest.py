"""tests/moa 共享 fixture 与辅助函数。

移植 Hermes ``tests/run_agent/test_moa_loop_mode.py`` 的 fixture 套路，但适配 Spirit
的 transport seam：

- **假响应**：``fake_response(...)`` 构造 ``TransportResponse`` 形状（SimpleNamespace
  with ``.content`` / ``.usage`` dict / ``.tool_calls`` / ``.finish_reason`` / ``.model``）。
  ``moa_loop`` 的 ``_extract_text`` / ``_usage_from_response`` / ``_adapt_to_chat_response``
  都用 getattr 读这些字段，故 SimpleNamespace 足够。
- **harness** fixture：monkeypatch ``moa_loop._call_slot`` / ``_call_slot_stream`` /
  ``_load_moa_config``（+ ``commands._load_moa_config``），记录每次调用并按 task 返回
  可配置的假响应。这是大多数 loop/command 测试的快速注入点（无文件 I/O、无网络）。
- **moa_home** fixture：把 ``SPIRIT_HOME`` 隔离到 ``tmp_path``（对标 Hermes 的
  ``HERMES_HOME`` + ``monkeypatch.setenv``），配合 ``write_moa_yaml`` 写真实
  ``config.yaml``，用于验证「真实配置路径」端到端（trace 持久化、真实预设解析）。
  ``load_config()`` 无缓存、``get_config_path()`` 调用时查 ``SPIRIT_HOME`` 全局，故
  monkeypatch 模块全局即生效（等价 q3 的 ``checkpoint_home`` 模式）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from spirit.moa import moa_loop


# ---------------------------------------------------------------------------
# 假响应 / 假流式事件
# ---------------------------------------------------------------------------

def fake_response(
    content: str = "ok",
    *,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    prompt: int = 0,
    completion: int = 0,
    total: Optional[int] = None,
    model: str = "fake-model",
    finish_reason: str = "stop",
) -> SimpleNamespace:
    """构造一个 ``TransportResponse`` 形状的假响应。

    ``tool_calls`` 是 Spirit transport 的 ``[{"id","name","args"}]`` dict 形态。
    ``usage`` 是 ``{prompt_tokens, completion_tokens, total_tokens}`` dict。
    """
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else prompt + completion,
    }
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=usage,
        finish_reason=finish_reason,
        model=model,
    )


def content_event(text: str) -> Dict[str, Any]:
    return {"type": "content", "text": text}


def tool_call_event(name: str, args: Any = None, call_id: str = "call_0") -> Dict[str, Any]:
    return {"type": "tool_call", "id": call_id, "name": name, "args": args or {}}


def done_event(prompt: int = 0, completion: int = 0, total: Optional[int] = None) -> Dict[str, Any]:
    return {
        "type": "done",
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total if total is not None else prompt + completion,
        },
    }


# ---------------------------------------------------------------------------
# 默认测试预设（raw moa 配置节形态，对标 Hermes _ref_config）
# ---------------------------------------------------------------------------

def make_config(
    *,
    default_preset: str = "review",
    presets: Optional[Dict[str, Any]] = None,
    save_traces: bool = False,
) -> Dict[str, Any]:
    """构造一个 raw ``moa`` 配置节（``_load_moa_config`` 的返回形态）。"""
    if presets is None:
        presets = {
            "review": {
                "reference_models": [
                    {"provider": "openai-codex", "model": "gpt-5.5"},
                    {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                ],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            },
        }
    cfg: Dict[str, Any] = {"default_preset": default_preset, "presets": presets}
    if save_traces:
        cfg["save_traces"] = True
    return cfg


# ---------------------------------------------------------------------------
# harness：monkeypatch transport seam + 配置注入
# ---------------------------------------------------------------------------

class MoAHarness:
    """把 ``moa_loop`` 的 transport seam 与配置读替换为可控假件，并记录调用。

    属性::

        calls          所有 _call_slot 调用记录（dict: slot/messages/task/temperature/max_tokens/tools）
        stream_calls   所有 _call_slot_stream 调用记录
        reference_calls  task=="moa_reference" 的子集
        aggregator_calls task=="moa_aggregator" 的子集

    方法::

        set_config(cfg)      替换 _load_moa_config 的返回
        set_responses(fn)    fn(rec)->response|None，自定义每次 _call_slot 的返回
        set_stream(fn)       fn(rec)->events_iterator，自定义 _call_slot_stream 的返回
    """

    def __init__(self, monkeypatch, config: Optional[Dict[str, Any]] = None):
        self.calls: List[Dict[str, Any]] = []
        self.stream_calls: List[Dict[str, Any]] = []
        self._config = config if config is not None else make_config()
        self._on_call = None
        self._on_stream = None

        monkeypatch.setattr(moa_loop, "_load_moa_config", lambda: self._config)
        monkeypatch.setattr(moa_loop, "_call_slot", self._fake_call_slot)
        monkeypatch.setattr(moa_loop, "_call_slot_stream", self._fake_call_slot_stream)
        # 命令层有自己同名的 _load_moa_config seam，一并 patch 保持一致。
        from spirit.moa import commands as moa_commands

        monkeypatch.setattr(moa_commands, "_load_moa_config", lambda: self._config)

    # -- 配置 ---------------------------------------------------------------
    def set_config(self, config: Dict[str, Any]) -> None:
        self._config = config

    def set_responses(self, on_call) -> None:
        self._on_call = on_call

    def set_stream(self, on_stream) -> None:
        self._on_stream = on_stream

    # -- 假 seam ------------------------------------------------------------
    def _fake_call_slot(
        self, slot, messages, *, task="moa_reference",
        temperature=None, max_tokens=None, tools=None,
    ):
        rec = {
            "slot": slot, "messages": messages, "task": task,
            "temperature": temperature, "max_tokens": max_tokens, "tools": tools,
        }
        self.calls.append(rec)
        if self._on_call is not None:
            r = self._on_call(rec)
            if r is not None:
                return r
        if task == "moa_reference":
            return fake_response(f"advice from {slot.get('model')}")
        return fake_response("aggregator acted")

    def _fake_call_slot_stream(
        self, slot, messages, *, task="moa_aggregator",
        temperature=None, max_tokens=None, tools=None,
    ):
        rec = {
            "slot": slot, "messages": messages, "task": task,
            "temperature": temperature, "max_tokens": max_tokens, "tools": tools,
        }
        self.stream_calls.append(rec)
        if self._on_stream is not None:
            return self._on_stream(rec)
        return iter([content_event("streamed "), content_event("answer"), done_event(1, 2)])

    # -- 便捷视图 -----------------------------------------------------------
    @property
    def reference_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["task"] == "moa_reference"]

    @property
    def aggregator_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["task"] == "moa_aggregator"]


@pytest.fixture
def harness(monkeypatch):
    """默认 harness（review 预设，2 参考 + 1 聚合器，save_traces 关）。"""
    return MoAHarness(monkeypatch)


@pytest.fixture
def make_harness(monkeypatch):
    """工厂 fixture：用自定义配置构造 harness。"""
    def _make(config: Optional[Dict[str, Any]] = None) -> MoAHarness:
        return MoAHarness(monkeypatch, config)
    return _make


# ---------------------------------------------------------------------------
# moa_home：真实 SPIRIT_HOME + config.yaml 路径
# ---------------------------------------------------------------------------

@pytest.fixture
def moa_home(tmp_path, monkeypatch):
    """把 SPIRIT_HOME 隔离到 tmp_path（对标 Hermes HERMES_HOME）。

    ``load_config()`` 无缓存且 ``get_config_path()`` 调用时查模块全局 ``SPIRIT_HOME``，
    故 monkeypatch 该全局后，写 ``tmp_path/config.yaml`` 即被真实配置路径读到。
    """
    import spirit.config as config

    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    return tmp_path


def write_moa_yaml(home, yaml_text: str) -> None:
    """把一段 YAML 写到 ``home/config.yaml``。"""
    (home / "config.yaml").write_text(yaml_text.strip(), encoding="utf-8")
