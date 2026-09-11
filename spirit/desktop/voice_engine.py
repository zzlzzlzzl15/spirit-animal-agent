"""语音引擎 — STT (语音识别) + TTS (语音合成)。

支持：
- STT: faster-whisper 本地模型 / 浏览器 Web Speech API
- TTS: Edge-TTS（免费云端）/ pyttsx3（本地离线）
- 音频流通过 WebSocket 传输

架构：
    前端录音 → WebSocket 音频流 → voice_engine.py → Whisper STT
    前端 ← WebSocket 音频流 ← voice_engine.py ← Edge-TTS TTS
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class VoiceConfig:
    """语音引擎配置。"""

    # STT
    stt_backend: str = "whisper"  # whisper / webspeech
    whisper_model: str = "base"   # tiny / base / small / medium / large
    whisper_device: str = "auto"  # auto / cpu / cuda
    stt_language: str = "zh"      # 识别语言

    # TTS
    tts_backend: str = "edge"     # edge / pyttsx3
    tts_voice: str = "zh-CN-XiaoxiaoNeural"  # Edge-TTS 语音
    tts_rate: int = 0             # 语速调整（百分比，0=正常）
    tts_volume: int = 0           # 音量调整（百分比，0=正常）

    # 唤醒词（可选）
    wake_word: str = ""           # 空字符串=不启用唤醒词

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        """从环境变量加载配置。"""
        cfg = cls()
        cfg.stt_backend = os.environ.get("SPIRIT_STT_BACKEND", cfg.stt_backend)
        cfg.whisper_model = os.environ.get("SPIRIT_WHISPER_MODEL", cfg.whisper_model)
        cfg.stt_language = os.environ.get("SPIRIT_STT_LANGUAGE", cfg.stt_language)
        cfg.tts_backend = os.environ.get("SPIRIT_TTS_BACKEND", cfg.tts_backend)
        cfg.tts_voice = os.environ.get("SPIRIT_TTS_VOICE", cfg.tts_voice)
        cfg.wake_word = os.environ.get("SPIRIT_WAKE_WORD", cfg.wake_word)
        return cfg


# ---------------------------------------------------------------------------
# STT 结果
# ---------------------------------------------------------------------------

@dataclass
class STTResult:
    """语音识别结果。"""

    text: str
    language: str = ""
    confidence: float = 0.0
    duration_ms: float = 0.0
    error: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def is_success(self) -> bool:
        return bool(self.text) and not self.error


# ---------------------------------------------------------------------------
# TTS 结果
# ---------------------------------------------------------------------------

@dataclass
class TTSResult:
    """语音合成结果。"""

    audio_data: bytes = b""
    format: str = "mp3"         # mp3 / wav
    duration_ms: float = 0.0
    voice: str = ""
    error: str = ""

    @property
    def is_success(self) -> bool:
        return bool(self.audio_data) and not self.error


# ---------------------------------------------------------------------------
# 语音引擎
# ---------------------------------------------------------------------------

class VoiceEngine:
    """语音引擎 — 统一的 STT/TTS 接口。

    支持多种后端，按需延迟加载：
    - faster-whisper（STT）
    - edge-tts（TTS）
    - pyttsx3（TTS 离线）
    """

    def __init__(self, config: VoiceConfig = None):
        self.config = config or VoiceConfig.from_env()
        self._whisper_model = None
        self._tts_engine = None
        self._initialized = False

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _ensure_initialized(self) -> None:
        """延迟初始化引擎。"""
        if self._initialized:
            return
        self._initialized = True
        logger.info(
            "语音引擎初始化: STT=%s (%s), TTS=%s (%s)",
            self.config.stt_backend,
            self.config.whisper_model,
            self.config.tts_backend,
            self.config.tts_voice,
        )

    def _get_whisper_model(self):
        """获取 Whisper 模型（延迟加载）。"""
        if self._whisper_model is not None:
            return self._whisper_model

        try:
            from faster_whisper import WhisperModel
            self._whisper_model = WhisperModel(
                self.config.whisper_model,
                device=self.config.whisper_device if self.config.whisper_device != "auto" else "cpu",
                compute_type="int8",
            )
            logger.info("Whisper 模型已加载: %s", self.config.whisper_model)
        except ImportError:
            logger.warning("faster-whisper 未安装，STT 不可用")
            self._whisper_model = False
        except Exception as exc:
            logger.warning("Whisper 加载失败: %s", exc)
            self._whisper_model = False

        return self._whisper_model

    # ------------------------------------------------------------------
    # STT — 语音识别
    # ------------------------------------------------------------------

    async def transcribe(self, audio_data: bytes, *, format: str = "wav") -> STTResult:
        """将音频数据转为文本。

        Args:
            audio_data: 音频原始数据
            format: 音频格式（wav/mp3/webm）

        Returns:
            STTResult
        """
        self._ensure_initialized()
        start = time.monotonic()

        if self.config.stt_backend == "whisper":
            return await self._transcribe_whisper(audio_data, format, start)
        else:
            return STTResult(
                text="",
                error=f"不支持的 STT 后端: {self.config.stt_backend}",
            )

    async def _transcribe_whisper(
        self, audio_data: bytes, format: str, start: float
    ) -> STTResult:
        """使用 faster-whisper 进行语音识别。"""
        model = self._get_whisper_model()
        if not model or model is False:
            return STTResult(
                text="",
                error="Whisper 模型不可用，请安装 faster-whisper",
            )

        # 写入临时文件
        tmp = tempfile.NamedTemporaryFile(
            suffix=f".{format}", delete=False
        )
        try:
            tmp.write(audio_data)
            tmp.flush()
            tmp.close()

            # 在线程池中执行推理（避免阻塞事件循环）
            loop = asyncio.get_running_loop()
            
            # 添加超时保护（默认 30 秒，从配置读取）
            from spirit.config import get_config_value
            stt_timeout = get_config_value("timeouts.stt_transcribe", 30.0)
            
            future = loop.run_in_executor(
                None, lambda: self._do_transcribe(model, tmp.name)
            )
            result = await asyncio.wait_for(future, timeout=stt_timeout)
            result.duration_ms = (time.monotonic() - start) * 1000
            return result
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _do_transcribe(self, model, audio_path: str) -> STTResult:
        """在线程中执行 Whisper 推理。"""
        try:
            segments, info = model.transcribe(
                audio_path,
                language=self.config.stt_language or None,
                beam_size=5,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return STTResult(
                text=text,
                language=info.language if hasattr(info, "language") else "",
                confidence=info.avg_logprob if hasattr(info, "avg_logprob") else 0,
            )
        except Exception as exc:
            return STTResult(text="", error=str(exc))

    # ------------------------------------------------------------------
    # TTS — 语音合成
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> TTSResult:
        """将文本转为语音。

        Args:
            text: 要合成的文本

        Returns:
            TTSResult（包含音频数据）
        """
        self._ensure_initialized()

        if not text.strip():
            return TTSResult(error="文本为空")

        if self.config.tts_backend == "edge":
            return await self._tts_edge(text)
        elif self.config.tts_backend == "pyttsx3":
            return await self._tts_pyttsx3(text)
        else:
            return TTSResult(error=f"不支持的 TTS 后端: {self.config.tts_backend}")

    async def _tts_edge(self, text: str) -> TTSResult:
        """使用 Edge-TTS 进行语音合成。"""
        try:
            import edge_tts
        except ImportError:
            return TTSResult(error="edge-tts 未安装，请运行: pip install edge-tts")

        try:
            communicate = edge_tts.Communicate(
                text,
                self.config.tts_voice,
            )

            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])

            audio_data = b"".join(audio_chunks)
            return TTSResult(
                audio_data=audio_data,
                format="mp3",
                voice=self.config.tts_voice,
            )
        except Exception as exc:
            return TTSResult(error=f"Edge-TTS 合成失败: {exc}")

    async def _tts_pyttsx3(self, text: str) -> TTSResult:
        """使用 pyttsx3 进行离线语音合成。"""
        try:
            import pyttsx3
        except ImportError:
            return TTSResult(error="pyttsx3 未安装，请运行: pip install pyttsx3")

        try:
            engine = pyttsx3.init()
            if self.config.tts_rate:
                engine.setProperty("rate", 200 + self.config.tts_rate)
            if self.config.tts_volume:
                vol = max(0, min(1.0, 1.0 + self.config.tts_volume / 100))
                engine.setProperty("volume", vol)

            # pyttsx3 只能直接播放，需要录制到内存
            # 这里使用临时文件
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()

            loop = asyncio.get_running_loop()
            
            # 添加超时保护（默认 30 秒，从配置读取）
            from spirit.config import get_config_value
            tts_timeout = get_config_value("timeouts.tts_synthesize", 30.0)
            
            future = loop.run_in_executor(
                None,
                lambda: self._do_pyttsx3(engine, text, tmp.name),
            )
            await asyncio.wait_for(future, timeout=tts_timeout)

            audio_data = Path(tmp.name).read_bytes()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

            return TTSResult(
                audio_data=audio_data,
                format="wav",
                voice="pyttsx3",
            )
        except Exception as exc:
            return TTSResult(error=f"pyttsx3 合成失败: {exc}")

    @staticmethod
    def _do_pyttsx3(engine, text: str, output_path: str) -> None:
        """在线程中执行 pyttsx3 合成。"""
        engine.save_to_file(text, output_path)
        engine.runAndWait()

    # ------------------------------------------------------------------
    # 唤醒词检测
    # ------------------------------------------------------------------

    def check_wake_word(self, text: str) -> bool:
        """检测文本中是否包含唤醒词。"""
        if not self.config.wake_word:
            return True  # 未配置唤醒词，始终通过
        return self.config.wake_word.lower() in text.lower()

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    async def listen_and_respond(self, audio_data: bytes) -> dict:
        """完整的语音问答流程：STT → Agent → TTS。

        Returns:
            {"text": "识别文本", "response": "回复文本", "audio": bytes}
        """
        # STT
        stt = await self.transcribe(audio_data)
        if stt.is_empty:
            return {
                "text": "",
                "response": stt.error or "未能识别语音",
                "audio": b"",
            }

        # 唤醒词检测
        if not self.check_wake_word(stt.text):
            return {
                "text": stt.text,
                "response": "",
                "audio": b"",
            }

        # Agent 对话（需要外部注入 agent 实例）
        response_text = f"收到: {stt.text}"

        # TTS
        tts = await self.synthesize(response_text)

        return {
            "text": stt.text,
            "response": response_text,
            "audio": tts.audio_data if tts.is_success else b"",
            "error": tts.error,
        }

    def get_available_backends(self) -> dict:
        """检测可用的后端。"""
        backends = {
            "stt": [],
            "tts": [],
        }

        # STT
        try:
            import faster_whisper
            backends["stt"].append("whisper")
        except ImportError:
            pass

        # TTS
        try:
            import edge_tts
            backends["tts"].append("edge")
        except ImportError:
            pass

        try:
            import pyttsx3
            backends["tts"].append("pyttsx3")
        except ImportError:
            pass

        return backends


__all__ = [
    "VoiceEngine",
    "VoiceConfig",
    "STTResult",
    "TTSResult",
]
