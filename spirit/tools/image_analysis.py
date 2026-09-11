"""图片分析工具 — analyze_image（使用 LLM 视觉能力分析图片）。

参考 Hermes 的 vision_tools.py 设计：
- 支持本地图片和 URL
- 通过 OpenAI Vision API 分析
- 适合 UI 截图分析、图表解读、错误截图诊断
"""

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

ANALYZE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": (
            "使用 AI 视觉能力分析图片内容。\n\n"
            "支持：\n"
            "- 本地图片文件（png/jpg/gif/webp）\n"
            "- 图片 URL\n\n"
            "适用场景：\n"
            "- 分析 UI 截图中的问题\n"
            "- 解读图表/流程图\n"
            "- 识别错误截图中的异常\n"
            "- 描述图片内容"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "图片来源（文件路径或 URL）",
                },
                "question": {
                    "type": "string",
                    "description": "关于图片的问题（可选，默认描述图片内容）",
                },
                "detail": {
                    "type": "string",
                    "description": "分析详细度: low / auto / high（默认 auto）",
                    "enum": ["low", "auto", "high"],
                },
            },
            "required": ["source"],
        },
    },
}


def _analyze_image_impl(
    source: str,
    question: str = None,
    detail: str = "auto",
) -> str:
    """分析图片。"""
    prompt = question or "请详细描述这张图片的内容。如果有代码、文字或错误信息，请一并提取。"

    # 构建图片内容
    image_content = None

    if source.startswith(("http://", "https://")):
        # URL 图片
        image_content = {"type": "image_url", "image_url": {"url": source, "detail": detail}}
    else:
        # 本地文件
        img_path = Path(source).resolve()
        if not img_path.exists():
            return json.dumps({"error": f"文件不存在: {source}"})

        if img_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return json.dumps({
                "error": f"不支持的格式: {img_path.suffix}",
                "supported": list(SUPPORTED_EXTENSIONS),
            })

        file_size = img_path.stat().st_size
        if file_size > MAX_IMAGE_SIZE:
            return json.dumps({
                "error": f"图片过大: {file_size / 1024 / 1024:.1f}MB（最大 {MAX_IMAGE_SIZE // 1024 // 1024}MB）",
            })

        # 读取并编码为 base64
        mime_type = mimetypes.guess_type(str(img_path))[0] or "image/png"
        with open(img_path, "rb") as f:
            b64_data = base64.standard_b64encode(f.read()).decode("utf-8")

        data_url = f"data:{mime_type};base64,{b64_data}"
        image_content = {"type": "image_url", "image_url": {"url": data_url, "detail": detail}}

    # 调用 LLM Vision API
    try:
        from openai import OpenAI
        import os

        from spirit.config import load_config
        cfg = load_config()
        llm_cfg = cfg.get("llm", {})

        client = OpenAI(
            api_key=llm_cfg.get("api_key") or os.getenv("SPIRIT_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            base_url=llm_cfg.get("base_url") or os.getenv("SPIRIT_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")),
        )

        response = client.chat.completions.create(
            model=llm_cfg.get("model") or "gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        image_content,
                    ],
                }
            ],
            max_tokens=llm_cfg.get("image_analysis_max_tokens", 1000),
        )

        result = response.choices[0].message.content or ""

        return json.dumps({
            "source": source,
            "question": prompt,
            "analysis": result,
            "model": "gpt-4o",
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": f"图片分析失败: {e}"})


registry.register(
    name="analyze_image",
    toolset="vision",
    schema=ANALYZE_IMAGE_SCHEMA,
    handler=_analyze_image_impl,
    description="AI 图片分析",
    emoji="🖼️",
    max_result_size_chars=5000,  # 图像分析结果较小
)
