"""语音工具 — Spirit Agent。

合并自 Hermes:
- tts_tool.py: 文本转语音（TTS）
- transcription_tools.py: 语音转文本（STT）
- voice_mode.py: 语音模式
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# TTS — 文本转语音
# ============================================================================

TTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tts",
        "description": "将文本转换为语音音频文件。支持多种 TTS 引擎。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要转换的文本"},
                "voice": {
                    "type": "string",
                    "description": "语音名称（可选）",
                },
                "output_path": {
                    "type": "string",
                    "description": "输出文件路径（可选，默认临时文件）",
                },
            },
            "required": ["text"],
        },
    },
}


def _handle_tts(args: Dict[str, Any], **kwargs) -> str:
    text = args.get("text", "")
    if not text:
        return json.dumps({"error": "text 不能为空"})

    voice = args.get("voice", "")
    output_path = args.get("output_path")

    # 尝试 ElevenLabs
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "")
    if elevenlabs_key:
        return _tts_elevenlabs(text, voice, output_path, elevenlabs_key)

    # 尝试 OpenAI
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        return _tts_openai(text, voice, output_path, openai_key)

    return json.dumps({
        "success": False,
        "error": "未配置 TTS 引擎。设置 ELEVENLABS_API_KEY 或 OPENAI_API_KEY。",
    })


def _tts_openai(text: str, voice: str, output_path: Optional[str], api_key: str) -> str:
    try:
        import requests
        voice = voice or "alloy"
        if not output_path:
            output_path = str(Path(tempfile.gettempdir()) / f"spirit_tts_{os.getpid()}.mp3")

        response = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "tts-1", "input": text, "voice": voice},
            timeout=60,
        )
        if response.status_code != 200:
            return json.dumps({"success": False, "error": f"OpenAI TTS 错误: {response.status_code}"})

        Path(output_path).write_bytes(response.content)
        return json.dumps({"success": True, "audio_path": output_path, "provider": "openai"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _tts_elevenlabs(text: str, voice: str, output_path: Optional[str], api_key: str) -> str:
    try:
        import requests
        voice_id = voice or "21m00Tcm4TlvDq6ikW1r"
        if not output_path:
            output_path = str(Path(tempfile.gettempdir()) / f"spirit_tts_{os.getpid()}.mp3")

        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key},
            json={"text": text, "model_id": "eleven_monolingual_v1"},
            timeout=60,
        )
        if response.status_code != 200:
            return json.dumps({"success": False, "error": f"ElevenLabs TTS 错误: {response.status_code}"})

        Path(output_path).write_bytes(response.content)
        return json.dumps({"success": True, "audio_path": output_path, "provider": "elevenlabs"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_tts_requirements() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY") or os.getenv("OPENAI_API_KEY"))


registry.register(
    name="tts",
    toolset="voice",
    schema=TTS_SCHEMA,
    handler=_handle_tts,
    check_fn=check_tts_requirements,
    emoji="🔊",
)


# ============================================================================
# STT — 语音转文本
# ============================================================================

TRANSCRIBE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "transcribe",
        "description": "将音频文件转录为文本。支持多种 STT 提供商。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "音频文件路径"},
                "language": {
                    "type": "string",
                    "description": "语言代码（如 en, zh, ja）",
                },
            },
            "required": ["file_path"],
        },
    },
}


def _handle_transcribe(args: Dict[str, Any], **kwargs) -> str:
    file_path = args.get("file_path", "")
    if not file_path:
        return json.dumps({"error": "file_path 不能为空"})

    audio_path = Path(file_path)
    if not audio_path.exists():
        return json.dumps({"error": f"文件不存在: {file_path}"})

    supported = {".mp3", ".mp4", ".wav", ".m4a", ".webm", ".ogg", ".flac"}
    if audio_path.suffix.lower() not in supported:
        return json.dumps({"error": f"不支持的格式: {audio_path.suffix}"})

    # 尝试 Groq（免费）
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        return _stt_groq(file_path, groq_key)

    # 尝试 OpenAI
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        return _stt_openai(file_path, openai_key)

    return json.dumps({
        "success": False,
        "error": "未配置 STT 引擎。设置 GROQ_API_KEY（免费）或 OPENAI_API_KEY。",
    })


def _stt_groq(file_path: str, api_key: str) -> str:
    try:
        import requests
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": open(file_path, "rb")},
            data={"model": "whisper-large-v3-turbo", "response_format": "text"},
            timeout=120,
        )
        if response.status_code != 200:
            return json.dumps({"success": False, "error": f"Groq STT 错误: {response.status_code}"})
        return json.dumps({"success": True, "transcript": response.text.strip(), "provider": "groq"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _stt_openai(file_path: str, api_key: str) -> str:
    try:
        import requests
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": open(file_path, "rb")},
            data={"model": "whisper-1", "response_format": "text"},
            timeout=120,
        )
        if response.status_code != 200:
            return json.dumps({"success": False, "error": f"OpenAI STT 错误: {response.status_code}"})
        return json.dumps({"success": True, "transcript": response.text.strip(), "provider": "openai"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_stt_requirements() -> bool:
    return bool(os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY"))


registry.register(
    name="transcribe",
    toolset="voice",
    schema=TRANSCRIBE_SCHEMA,
    handler=_handle_transcribe,
    check_fn=check_stt_requirements,
    emoji="🎤",
)
