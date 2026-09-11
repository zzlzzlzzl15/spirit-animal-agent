"""媒体生成工具 — Spirit Agent。

合并自 Hermes:
- image_generation_tool.py: 图片生成（FAL.ai / OpenAI / 多模型）
- video_generation_tool.py: 视频生成（插件化）
- fal_common.py: FAL 公共工具
"""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# 图片生成（简化版 — 支持 FAL.ai 和 OpenAI）
# ============================================================================

VALID_ASPECT_RATIOS = ("landscape", "square", "portrait")
DEFAULT_ASPECT_RATIO = "landscape"

# FAL 模型目录（简化版）
FAL_MODELS = {
    "fal-ai/flux-2/klein/9b": {
        "display": "FLUX 2 Klein 9B",
        "speed": "<1s",
        "price": "$0.006/MP",
    },
    "fal-ai/flux-2-pro": {
        "display": "FLUX 2 Pro",
        "speed": "~6s",
        "price": "$0.03/MP",
    },
    "fal-ai/gpt-image-1.5": {
        "display": "GPT Image 1.5",
        "speed": "~15s",
        "price": "$0.034/image",
    },
}

DEFAULT_MODEL = "fal-ai/flux-2/klein/9b"
IMAGE_CACHE_DIR = Path.home() / ".spirit" / "images"


def _resolve_fal_model() -> tuple:
    """解析当前 FAL 模型。"""
    model_id = os.getenv("FAL_IMAGE_MODEL", "").strip()
    if model_id and model_id in FAL_MODELS:
        return model_id, FAL_MODELS[model_id]
    return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]


def _build_fal_payload(
    model_id: str,
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """构建 FAL 请求 payload。"""
    aspect = aspect_ratio.lower() if aspect_ratio else DEFAULT_ASPECT_RATIO
    if aspect not in VALID_ASPECT_RATIOS:
        aspect = DEFAULT_ASPECT_RATIO

    size_map = {
        "landscape": "landscape_16_9",
        "square": "square_hd",
        "portrait": "portrait_16_9",
    }

    payload = {
        "prompt": prompt.strip(),
        "image_size": size_map.get(aspect, "landscape_16_9"),
        "num_inference_steps": 4,
        "output_format": "png",
        "enable_safety_checker": False,
    }
    if seed is not None:
        payload["seed"] = seed
    return payload


def image_generate(
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """生成图片。"""
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    fal_key = os.getenv("FAL_KEY", "").strip()
    if not fal_key:
        return {
            "success": False,
            "image": None,
            "error": "FAL_KEY 未设置。在 https://fal.ai 获取免费 API key。",
        }

    model_id, meta = _resolve_fal_model()
    payload = _build_fal_payload(model_id, prompt, aspect_ratio, seed)

    try:
        import requests
        response = requests.post(
            f"https://queue.fal.run/{model_id}",
            headers={
                "Authorization": f"Key {fal_key}",
                "Content-Type": "application/json",
                "X-FAL-Request-Id": str(uuid.uuid4()),
            },
            json={"input": payload},
            timeout=120,
        )

        if response.status_code != 200:
            return {
                "success": False,
                "image": None,
                "error": f"FAL API 错误 ({response.status_code}): {response.text[:300]}",
            }

        result = response.json()
        images = result.get("images", [])
        if not images:
            return {"success": False, "image": None, "error": "未生成图片"}

        image_url = images[0].get("url", "")
        return {
            "success": True,
            "image": image_url,
            "model": meta.get("display", model_id),
            "width": images[0].get("width", 0),
            "height": images[0].get("height", 0),
        }

    except ImportError:
        return {"success": False, "image": None, "error": "requests 库未安装"}
    except Exception as exc:
        return {"success": False, "image": None, "error": str(exc)}


IMAGE_GENERATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "image_generate",
        "description": (
            "从文本提示生成高质量图片。支持多种 FAL.ai 模型。\n\n"
            "需要设置 FAL_KEY 环境变量（在 https://fal.ai 免费获取）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "详细的图片描述文本",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": list(VALID_ASPECT_RATIOS),
                    "description": "图片宽高比: landscape(16:9), square(1:1), portrait(9:16)",
                    "default": DEFAULT_ASPECT_RATIO,
                },
                "seed": {
                    "type": "integer",
                    "description": "随机种子（可重现生成）",
                },
            },
            "required": ["prompt"],
        },
    },
}


def _handle_image_generate(args: Dict[str, Any], **kwargs) -> str:
    prompt = args.get("prompt", "")
    if not prompt:
        return json.dumps({"error": "prompt 不能为空"})
    aspect_ratio = args.get("aspect_ratio", DEFAULT_ASPECT_RATIO)
    seed = args.get("seed")
    result = image_generate(prompt, aspect_ratio, seed)
    return json.dumps(result, ensure_ascii=False)


def check_image_requirements() -> bool:
    return bool(os.getenv("FAL_KEY"))


registry.register(
    name="image_generate",
    toolset="media",
    schema=IMAGE_GENERATE_SCHEMA,
    handler=_handle_image_generate,
    check_fn=check_image_requirements,
    emoji="🎨",
)


# ============================================================================
# 视频生成（简化版 — 插件化架构）
# ============================================================================

VIDEO_GENERATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "video_generate",
        "description": (
            "从文本提示生成视频（text-to-video），或从图片生成视频（image-to-video）。\n\n"
            "需要配置视频生成后端（通过插件系统）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "视频描述文本",
                },
                "image_url": {
                    "type": "string",
                    "description": "可选的源图片 URL（image-to-video）",
                },
                "duration": {
                    "type": "integer",
                    "description": "视频时长（秒）",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "宽高比（如 16:9, 9:16, 1:1）",
                    "default": "16:9",
                },
            },
            "required": ["prompt"],
        },
    },
}


def _handle_video_generate(args: Dict[str, Any], **kwargs) -> str:
    return json.dumps({
        "success": False,
        "error": "视频生成需要配置后端插件。运行 `spirit plugins install video_gen/<name>` 安装。",
    })


registry.register(
    name="video_generate",
    toolset="media",
    schema=VIDEO_GENERATE_SCHEMA,
    handler=_handle_video_generate,
    check_fn=lambda: False,
    emoji="🎬",
)
