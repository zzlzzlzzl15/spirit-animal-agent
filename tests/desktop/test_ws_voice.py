"""tests/desktop/test_ws_voice.py — WebSocket 语音命令测试（3.4 语音交互）。

覆盖 ``transcribe_audio``（STT）、``synthesize_speech``（TTS，本轮新增）、
``voice_backends``（能力探测，本轮新增）、``voice_chat``（STT→Agent→TTS 一站式，
本轮新增）四条命令。语音引擎用桩替换，不加载任何真实模型。
"""

import asyncio
import base64

import pytest

from spirit.desktop.pet_engine import PetEngine
from spirit.desktop.voice_engine import STTResult, TTSResult, VoiceConfig, VoiceEngine
from spirit.desktop.ws_server import WSServer


class FakeVoiceEngine:
    """可控语音引擎桩。"""

    def __init__(self, *, stt=None, tts=None, backends=None, raise_on=None):
        self.stt = stt if stt is not None else STTResult(text="你好")
        self.tts = tts if tts is not None else TTSResult(
            audio_data=b"AUDIOBYTES", format="mp3", voice="zh-CN-XiaoxiaoNeural",
            duration_ms=1200,
        )
        self.backends = backends if backends is not None else {"stt": ["whisper"], "tts": ["edge"]}
        self.raise_on = raise_on or set()
        self.calls = []

    async def transcribe(self, audio_data, *, format="wav"):
        self.calls.append(("transcribe", len(audio_data), format))
        if "transcribe" in self.raise_on:
            raise RuntimeError("stt boom")
        return self.stt

    async def synthesize(self, text):
        self.calls.append(("synthesize", text))
        if "synthesize" in self.raise_on:
            raise RuntimeError("tts boom")
        return self.tts

    def get_available_backends(self):
        if "backends" in self.raise_on:
            raise RuntimeError("probe boom")
        return self.backends


@pytest.fixture
def server():
    return WSServer(PetEngine())


def with_engine(server, engine):
    server._voice_engine = engine
    return engine


# ---------------------------------------------------------------------------
# 命令注册
# ---------------------------------------------------------------------------

class TestVoiceCommandsRegistered:
    def test_commands_exist(self, server):
        actions = server.commands.list_actions()
        assert "transcribe_audio" in actions
        assert "synthesize_speech" in actions
        assert "voice_backends" in actions

    def test_handlers_are_callable(self, server):
        for name in ("transcribe_audio", "synthesize_speech", "voice_backends"):
            assert callable(server.commands.get(name))


# ---------------------------------------------------------------------------
# synthesize_speech — TTS
# ---------------------------------------------------------------------------

class TestSynthesizeSpeech:
    @pytest.mark.asyncio
    async def test_missing_text(self, server):
        handler = server.commands.get("synthesize_speech")
        assert (await handler(None, {})).get("error")
        assert (await handler(None, {"text": "   "})).get("error")

    @pytest.mark.asyncio
    async def test_returns_base64_audio(self, server):
        engine = with_engine(server, FakeVoiceEngine())
        handler = server.commands.get("synthesize_speech")
        result = await handler(None, {"text": "你好，我是小灵"})

        assert "error" not in result
        assert base64.b64decode(result["audio_base64"]) == b"AUDIOBYTES"
        assert result["format"] == "mp3"
        assert result["voice"] == "zh-CN-XiaoxiaoNeural"
        assert result["duration_ms"] == 1200
        assert engine.calls == [("synthesize", "你好，我是小灵")]

    @pytest.mark.asyncio
    async def test_wav_format_reported(self, server):
        with_engine(server, FakeVoiceEngine(
            tts=TTSResult(audio_data=b"WAV", format="wav")
        ))
        result = await server.commands.get("synthesize_speech")(None, {"text": "hi"})
        assert result["format"] == "wav"

    @pytest.mark.asyncio
    async def test_engine_error_propagated(self, server):
        with_engine(server, FakeVoiceEngine(
            tts=TTSResult(audio_data=b"", error="edge-tts 未安装")
        ))
        result = await server.commands.get("synthesize_speech")(None, {"text": "hi"})
        assert result.get("error") == "edge-tts 未安装"

    @pytest.mark.asyncio
    async def test_empty_audio_without_error(self, server):
        with_engine(server, FakeVoiceEngine(tts=TTSResult(audio_data=b"")))
        result = await server.commands.get("synthesize_speech")(None, {"text": "hi"})
        assert result.get("error")
        assert "audio_base64" not in result

    @pytest.mark.asyncio
    async def test_exception_caught(self, server):
        with_engine(server, FakeVoiceEngine(raise_on={"synthesize"}))
        result = await server.commands.get("synthesize_speech")(None, {"text": "hi"})
        assert "tts boom" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_base64_is_ascii_safe(self, server):
        """前端直接拼 data URL，base64 必须是纯 ASCII。"""
        with_engine(server, FakeVoiceEngine(
            tts=TTSResult(audio_data=bytes(range(256)), format="mp3")
        ))
        result = await server.commands.get("synthesize_speech")(None, {"text": "hi"})
        assert result["audio_base64"].isascii()
        assert "\n" not in result["audio_base64"]


# ---------------------------------------------------------------------------
# voice_backends — 能力探测
# ---------------------------------------------------------------------------

class TestVoiceBackends:
    @pytest.mark.asyncio
    async def test_returns_backends(self, server):
        with_engine(server, FakeVoiceEngine())
        result = await server.commands.get("voice_backends")(None, {})
        assert result["backends"] == {"stt": ["whisper"], "tts": ["edge"]}

    @pytest.mark.asyncio
    async def test_empty_backends(self, server):
        with_engine(server, FakeVoiceEngine(backends={"stt": [], "tts": []}))
        result = await server.commands.get("voice_backends")(None, {})
        assert result["backends"] == {"stt": [], "tts": []}

    @pytest.mark.asyncio
    async def test_exception_falls_back_to_empty(self, server):
        with_engine(server, FakeVoiceEngine(raise_on={"backends"}))
        result = await server.commands.get("voice_backends")(None, {})
        assert result["backends"] == {"stt": [], "tts": []}
        assert "probe boom" in result.get("error", "")


# ---------------------------------------------------------------------------
# transcribe_audio — STT
# ---------------------------------------------------------------------------

class TestTranscribeAudio:
    @pytest.mark.asyncio
    async def test_missing_audio(self, server):
        result = await server.commands.get("transcribe_audio")(None, {})
        assert result.get("error")

    @pytest.mark.asyncio
    async def test_returns_text(self, server):
        engine = with_engine(server, FakeVoiceEngine(
            stt=STTResult(text="打开终端", language="zh")
        ))
        payload = base64.b64encode(b"RIFF....").decode()
        result = await server.commands.get("transcribe_audio")(
            None, {"audio_base64": payload, "mime_type": "audio/wav"}
        )
        assert result["text"] == "打开终端"
        assert result["language"] == "zh"
        assert engine.calls[0][:2] == ("transcribe", 8)
        assert engine.calls[0][2] == "wav"

    @pytest.mark.asyncio
    async def test_mime_type_mapping(self, server):
        engine = with_engine(server, FakeVoiceEngine())
        payload = base64.b64encode(b"x").decode()
        handler = server.commands.get("transcribe_audio")
        await handler(None, {"audio_base64": payload, "mime_type": "audio/mpeg"})
        await handler(None, {"audio_base64": payload, "mime_type": "audio/ogg"})
        await handler(None, {"audio_base64": payload, "mime_type": "audio/webm"})
        assert [c[2] for c in engine.calls] == ["mp3", "ogg", "webm"]

    @pytest.mark.asyncio
    async def test_empty_recognition(self, server):
        with_engine(server, FakeVoiceEngine(stt=STTResult(text="  ")))
        result = await server.commands.get("transcribe_audio")(
            None, {"audio_base64": base64.b64encode(b"x").decode()}
        )
        assert result["text"] == ""
        assert result.get("message")

    @pytest.mark.asyncio
    async def test_engine_error(self, server):
        with_engine(server, FakeVoiceEngine(stt=STTResult(text="", error="Whisper 不可用")))
        result = await server.commands.get("transcribe_audio")(
            None, {"audio_base64": base64.b64encode(b"x").decode()}
        )
        assert result.get("error") == "Whisper 不可用"

    @pytest.mark.asyncio
    async def test_invalid_base64_caught(self, server):
        with_engine(server, FakeVoiceEngine())
        result = await server.commands.get("transcribe_audio")(
            None, {"audio_base64": "!!!not-base64!!!"}
        )
        assert result.get("error")


# ---------------------------------------------------------------------------
# voice_chat — STT → Agent → TTS 一站式
# ---------------------------------------------------------------------------

class FakeAgent:
    """同步 chat 接口的 Agent 桩（ws_server 会放线程池调用）。"""

    def __init__(self, reply="你好，我是小灵", as_dict=True):
        self.reply = reply
        self.as_dict = as_dict
        self.received = []

    def chat(self, message, *args, **kwargs):
        self.received.append(message)
        return {"response": self.reply} if self.as_dict else self.reply


def stubbed_engine(monkeypatch, *, stt_text="现在几点", tts_audio=b"REPLY_AUDIO"):
    """真实 VoiceEngine + 打桩后端（测试真实 listen_and_respond 管线）。"""
    engine = VoiceEngine(VoiceConfig())

    async def fake_transcribe(audio_data, *, format="wav"):
        return STTResult(text=stt_text)

    async def fake_synthesize(text):
        return TTSResult(audio_data=tts_audio, format="mp3", voice="v")

    monkeypatch.setattr(engine, "transcribe", fake_transcribe)
    monkeypatch.setattr(engine, "synthesize", fake_synthesize)
    return engine


class TestVoiceChat:
    @pytest.mark.asyncio
    async def test_command_registered(self, server):
        assert callable(server.commands.get("voice_chat"))

    @pytest.mark.asyncio
    async def test_missing_audio(self, server):
        result = await server.commands.get("voice_chat")(None, {})
        assert result.get("error")

    @pytest.mark.asyncio
    async def test_full_pipeline(self, monkeypatch, server):
        agent = FakeAgent()
        server.agent = agent
        with_engine(server, stubbed_engine(monkeypatch))

        payload = base64.b64encode(b"AUDIO").decode()
        result = await server.commands.get("voice_chat")(None, {"audio_base64": payload})

        assert agent.received == ["现在几点"]
        assert result["text"] == "现在几点"
        assert result["response"] == "你好，我是小灵"
        assert base64.b64decode(result["audio_base64"]) == b"REPLY_AUDIO"
        assert result["format"] == "mp3"
        assert result["error"] == ""

    @pytest.mark.asyncio
    async def test_agent_returning_plain_string(self, monkeypatch, server):
        server.agent = FakeAgent(reply="纯文本回复", as_dict=False)
        with_engine(server, stubbed_engine(monkeypatch))
        payload = base64.b64encode(b"AUDIO").decode()
        result = await server.commands.get("voice_chat")(None, {"audio_base64": payload})
        assert result["response"] == "纯文本回复"
        assert result["audio_base64"]

    @pytest.mark.asyncio
    async def test_without_agent_degrades_to_echo(self, monkeypatch, server):
        """未注入 Agent 时仍能跑通（回显），不应报错。"""
        server.agent = None
        with_engine(server, stubbed_engine(monkeypatch))
        payload = base64.b64encode(b"AUDIO").decode()
        result = await server.commands.get("voice_chat")(None, {"audio_base64": payload})
        assert result["text"] == "现在几点"
        assert result["response"].startswith("收到:")

    @pytest.mark.asyncio
    async def test_stt_failure_returns_error_without_audio(self, monkeypatch, server):
        server.agent = FakeAgent()
        engine = VoiceEngine(VoiceConfig())

        async def fake_transcribe(audio_data, *, format="wav"):
            return STTResult(text="", error="Whisper 不可用")

        monkeypatch.setattr(engine, "transcribe", fake_transcribe)
        with_engine(server, engine)

        payload = base64.b64encode(b"AUDIO").decode()
        result = await server.commands.get("voice_chat")(None, {"audio_base64": payload})
        assert result["error"] == "Whisper 不可用"
        assert result["response"] == "Whisper 不可用"
        assert "audio_base64" not in result
        assert server.agent.received == []  # STT 失败不该调 Agent

    @pytest.mark.asyncio
    async def test_invalid_base64_caught(self, server):
        with_engine(server, FakeVoiceEngine())
        result = await server.commands.get("voice_chat")(
            None, {"audio_base64": "@@@bad@@@"}
        )
        assert "解码失败" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_timeout_reported(self, monkeypatch, server):
        server.agent = FakeAgent()
        engine = VoiceEngine(VoiceConfig())

        async def slow_transcribe(audio_data, *, format="wav"):
            await asyncio.sleep(5)
            return STTResult(text="太晚了")

        monkeypatch.setattr(engine, "transcribe", slow_transcribe)
        monkeypatch.setattr(engine, "synthesize", None)  # 不应被调用
        with_engine(server, engine)

        import spirit.config as spirit_config
        monkeypatch.setattr(spirit_config, "get_config_value", lambda key, default=None: 0.01)

        payload = base64.b64encode(b"AUDIO").decode()
        result = await server.commands.get("voice_chat")(None, {"audio_base64": payload})
        assert result.get("timeout") is True
        assert result.get("error")
