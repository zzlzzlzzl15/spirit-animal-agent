"""tests/desktop/test_voice_engine.py — 语音引擎测试（3.4 语音交互）。

后端（faster-whisper / edge-tts / pyttsx3）均为可选依赖，测试全部用
monkeypatch 替换，不触发任何真实模型加载或网络请求。

重点覆盖 Phase 3.4 补齐的 ``listen_and_respond`` 真实管线（STT → Agent → TTS）。
"""

import asyncio

import pytest

from spirit.desktop.voice_engine import (
    STTResult,
    TTSResult,
    VoiceConfig,
    VoiceEngine,
)


def make_engine(**overrides) -> VoiceEngine:
    cfg = VoiceConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return VoiceEngine(cfg)


@pytest.fixture
def stubbed_io(monkeypatch):
    """把 STT/TTS 后端替换为可控桩，返回记录调用参数的容器。"""
    calls = {"stt": [], "tts": []}

    def install(engine: VoiceEngine, *, stt_text="你好", stt_error="",
                tts_audio=b"MP3DATA", tts_error=""):
        async def fake_transcribe(audio_data, *, format="wav"):
            calls["stt"].append({"bytes": audio_data, "format": format})
            return STTResult(text=stt_text, error=stt_error)

        async def fake_synthesize(text):
            calls["tts"].append(text)
            return TTSResult(audio_data=tts_audio, format="mp3", error=tts_error)

        monkeypatch.setattr(engine, "transcribe", fake_transcribe)
        monkeypatch.setattr(engine, "synthesize", fake_synthesize)
        return engine

    return {"calls": calls, "install": install}


# ---------------------------------------------------------------------------
# 配置与结果对象
# ---------------------------------------------------------------------------

class TestVoiceConfig:
    def test_defaults(self):
        cfg = VoiceConfig()
        assert cfg.stt_backend == "whisper"
        assert cfg.tts_backend == "edge"
        assert cfg.wake_word == ""

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_STT_BACKEND", "webspeech")
        monkeypatch.setenv("SPIRIT_TTS_BACKEND", "pyttsx3")
        monkeypatch.setenv("SPIRIT_WAKE_WORD", "小灵")
        monkeypatch.setenv("SPIRIT_TTS_VOICE", "zh-CN-YunxiNeural")
        cfg = VoiceConfig.from_env()
        assert cfg.stt_backend == "webspeech"
        assert cfg.tts_backend == "pyttsx3"
        assert cfg.wake_word == "小灵"
        assert cfg.tts_voice == "zh-CN-YunxiNeural"


class TestResultObjects:
    def test_stt_empty(self):
        assert STTResult(text="   ").is_empty is True
        assert STTResult(text="hi").is_empty is False

    def test_stt_success(self):
        assert STTResult(text="hi").is_success is True
        assert STTResult(text="hi", error="boom").is_success is False

    def test_tts_success(self):
        assert TTSResult(audio_data=b"x").is_success is True
        assert TTSResult(audio_data=b"").is_success is False
        assert TTSResult(audio_data=b"x", error="boom").is_success is False


# ---------------------------------------------------------------------------
# 后端不可用 / 不支持时的降级
# ---------------------------------------------------------------------------

class TestBackendFallback:
    @pytest.mark.asyncio
    async def test_transcribe_unsupported_backend(self):
        engine = make_engine(stt_backend="nope")
        result = await engine.transcribe(b"audio")
        assert result.is_empty is True
        assert "不支持的 STT 后端" in result.error

    @pytest.mark.asyncio
    async def test_transcribe_whisper_missing(self, monkeypatch):
        engine = make_engine()
        monkeypatch.setattr(engine, "_get_whisper_model", lambda: False)
        result = await engine.transcribe(b"audio")
        assert result.error
        assert "faster-whisper" in result.error

    @pytest.mark.asyncio
    async def test_synthesize_empty_text(self):
        engine = make_engine()
        result = await engine.synthesize("   ")
        assert result.is_success is False
        assert result.error == "文本为空"

    @pytest.mark.asyncio
    async def test_synthesize_unsupported_backend(self):
        engine = make_engine(tts_backend="nope")
        result = await engine.synthesize("你好")
        assert result.is_success is False
        assert "不支持的 TTS 后端" in result.error

    def test_get_available_backends_shape(self):
        backends = make_engine().get_available_backends()
        assert set(backends) >= {"stt", "tts"}
        assert isinstance(backends["stt"], list)
        assert isinstance(backends["tts"], list)


# ---------------------------------------------------------------------------
# 唤醒词
# ---------------------------------------------------------------------------

class TestWakeWord:
    def test_no_wake_word_always_passes(self):
        assert make_engine().check_wake_word("随便说点什么") is True

    def test_wake_word_matched_case_insensitive(self):
        engine = make_engine(wake_word="Spirit")
        assert engine.check_wake_word("hey spirit, hi") is True

    def test_wake_word_not_matched(self):
        engine = make_engine(wake_word="Spirit")
        assert engine.check_wake_word("你好") is False


# ---------------------------------------------------------------------------
# listen_and_respond — STT → Agent → TTS 完整管线
# ---------------------------------------------------------------------------

class TestListenAndRespond:
    @pytest.mark.asyncio
    async def test_full_pipeline_without_agent(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="今天天气如何")
        result = await engine.listen_and_respond(b"audio")
        assert result["text"] == "今天天气如何"
        assert result["response"] == "收到: 今天天气如何"
        assert result["audio"] == b"MP3DATA"
        assert result["format"] == "mp3"
        assert result["error"] == ""
        assert stubbed_io["calls"]["tts"] == ["收到: 今天天气如何"]

    @pytest.mark.asyncio
    async def test_sync_agent_callback(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")
        result = await engine.listen_and_respond(
            b"audio", agent_chat=lambda text: f"回复:{text}"
        )
        assert result["response"] == "回复:你好"
        assert result["audio"] == b"MP3DATA"

    @pytest.mark.asyncio
    async def test_async_agent_callback(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")

        async def agent(text):
            await asyncio.sleep(0)
            return f"异步回复:{text}"

        result = await engine.listen_and_respond(b"audio", agent_chat=agent)
        assert result["response"] == "异步回复:你好"

    @pytest.mark.asyncio
    async def test_agent_returning_dict(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")
        result = await engine.listen_and_respond(
            b"audio", agent_chat=lambda text: {"response": "字典回复"}
        )
        assert result["response"] == "字典回复"

    @pytest.mark.asyncio
    async def test_agent_returning_dict_text_key(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")
        result = await engine.listen_and_respond(
            b"audio", agent_chat=lambda text: {"text": "text 字段回复"}
        )
        assert result["response"] == "text 字段回复"

    @pytest.mark.asyncio
    async def test_agent_exception_is_caught(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")

        def bad_agent(text):
            raise RuntimeError("llm down")

        result = await engine.listen_and_respond(b"audio", agent_chat=bad_agent)
        assert "llm down" in result["response"]
        assert result["response"].startswith("抱歉")
        # 仍会合成语音，保证语音对话不中断
        assert result["audio"] == b"MP3DATA"

    @pytest.mark.asyncio
    async def test_stt_failure_short_circuits(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="", stt_error="无音频")
        result = await engine.listen_and_respond(b"", agent_chat=lambda t: "不该被调用")
        assert result["text"] == ""
        assert result["response"] == "无音频"
        assert result["audio"] == b""
        assert result["error"] == "无音频"
        assert stubbed_io["calls"]["tts"] == []

    @pytest.mark.asyncio
    async def test_wake_word_gate_skips_agent(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(wake_word="Spirit"), stt_text="随便聊聊")
        called = []
        result = await engine.listen_and_respond(
            b"audio", agent_chat=lambda t: called.append(t) or "回复"
        )
        assert called == []
        assert result["text"] == "随便聊聊"
        assert result["response"] == ""
        assert result["audio"] == b""
        assert stubbed_io["calls"]["tts"] == []

    @pytest.mark.asyncio
    async def test_wake_word_present_triggers_agent(self, stubbed_io):
        engine = stubbed_io["install"](
            make_engine(wake_word="Spirit"), stt_text="spirit 帮我查一下"
        )
        result = await engine.listen_and_respond(b"audio", agent_chat=lambda t: "好的")
        assert result["response"] == "好的"

    @pytest.mark.asyncio
    async def test_empty_agent_response_skips_tts(self, stubbed_io):
        engine = stubbed_io["install"](make_engine(), stt_text="你好")
        result = await engine.listen_and_respond(b"audio", agent_chat=lambda t: "")
        assert result["response"] == ""
        assert result["audio"] == b""
        assert result["error"] == "无回复内容"
        assert stubbed_io["calls"]["tts"] == []

    @pytest.mark.asyncio
    async def test_tts_failure_returns_empty_audio(self, stubbed_io):
        engine = stubbed_io["install"](
            make_engine(), stt_text="你好", tts_audio=b"", tts_error="TTS 引擎不可用"
        )
        result = await engine.listen_and_respond(b"audio", agent_chat=lambda t: "回复")
        assert result["response"] == "回复"
        assert result["audio"] == b""
        assert result["format"] == ""
        assert result["error"] == "TTS 引擎不可用"
