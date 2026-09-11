"""Spirit Desktop — 桌宠系统。

架构：
- Python 后端引擎（pet_engine + ws_server + tray_icon）
- Electron + Vue 3 前端（desktop-app/）
- WebSocket 双向通信

模块：
- pet_constants: 帧几何 + PetState 枚举
- pet_state:   Agent 活动 → PetState 映射
- pet_store:   精灵图存储/安装/切换
- pet_engine:  Pet 引擎核心
- ws_server:   WebSocket 桥接服务器
- tray_icon:   系统托盘图标
- system_status: 系统状态采集
- voice_engine:  语音引擎 (STT/TTS)
"""

from spirit.desktop.pet_engine import PetEngine
from spirit.desktop.pet_state import derive_pet_state
from spirit.desktop.pet_constants import PetState
from spirit.desktop.ws_server import WSServer, run_ws_server
from spirit.desktop.voice_engine import VoiceEngine
from spirit.desktop import system_status

__all__ = [
    "PetEngine",
    "PetState",
    "VoiceEngine",
    "WSServer",
    "derive_pet_state",
    "run_ws_server",
    "system_status",
]
