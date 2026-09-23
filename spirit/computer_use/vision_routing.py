"""Computer Use 捕获结果的视觉路由决策 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/vision_routing.py``：``computer_use(action='capture')``
返回带截图的多模态信封，作为工具结果交回**当前会话模型**。当主模型无视觉能力、或当前
provider 拒绝工具结果里的多模态内容时，截图会在 provider 边界触发 400/404，Agent 循环
报硬性工具失败。

本模块集中这个小策略决策：截图应作为多模态内容返回（主模型原生处理视觉），还是先经
辅助视觉管线预分析、让主模型只看到文本描述。

行为（对齐 Hermes）：

1. 用户显式配置了 ``auxiliary.vision``（provider/model/base_url 任一非空且非 "auto"）
   → 走辅助视觉管线（用户为专用视觉模型付费，通常就想用它）。
2. 否则，用户显式声明当前模型 ``supports_vision`` → True 返回 False（用多模态路径），
   False 返回 True（走辅助）。这是自定义 / 本地 VLM 路由的逃生舱。
3. 否则，若当前 provider+model 能在工具结果里携带图片**且**元数据报告 ``supports_vision``
   → 返回 False（多模态路径）。
4. 其余一切情形（非视觉主模型 / provider 不接受多模态工具结果 / 查询失败）→ 走辅助视觉，
   让主模型收到可据以行动的文本描述。

决策在元数据缺失或含糊时**故意 fail-closed**（倾向辅助路由）：把截图丢给读不了它的模型
是硬性失败，而走辅助只多花一次 LLM 调用并产出可用描述。

**可测试抽象层**：Spirit 无 models.dev / image_routing 元数据源，故三个查询
（用户声明、supports_vision、provider 是否接受工具结果图片）全部是**可注入 seam**，
默认实现只读 ``cfg``（无外部依赖），测试可注入任意真值表离线断言整条决策链。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# seam 类型别名。
UserDeclaredFn = Callable[[Optional[Dict[str, Any]], str, str], Optional[bool]]
SupportsVisionFn = Callable[[str, str, Optional[Dict[str, Any]]], Optional[bool]]
AcceptsToolImageFn = Callable[[str, str], Optional[bool]]


def _explicit_aux_vision_override(cfg: Optional[Dict[str, Any]]) -> bool:
    """``auxiliary.vision`` 携带非默认用户覆盖时为 True。

    ``provider: "auto"``、空值、缺失块都算**非**显式。
    """
    if not isinstance(cfg, dict):
        return False
    aux = cfg.get("auxiliary") or {}
    if not isinstance(aux, dict):
        return False
    vision = aux.get("vision") or {}
    if not isinstance(vision, dict):
        return False

    provider = str(vision.get("provider") or "").strip().lower()
    model = str(vision.get("model") or "").strip()
    base_url = str(vision.get("base_url") or "").strip()

    if provider in ("", "auto") and not model and not base_url:
        return False
    return True


def _default_user_declared(
    cfg: Optional[Dict[str, Any]], provider: str, model: str
) -> Optional[bool]:
    """默认：从 ``cfg`` 读用户显式声明的 ``supports_vision``（无则 None）。

    约定优先级：``models["<provider>/<model>"].supports_vision`` →
    ``models["<model>"].supports_vision`` → 顶层 ``model.supports_vision``。
    """
    if not isinstance(cfg, dict):
        return None
    models = cfg.get("models")
    if isinstance(models, dict):
        for key in (f"{provider}/{model}", model):
            entry = models.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("supports_vision"), bool):
                return entry["supports_vision"]
    top = cfg.get("model")
    if isinstance(top, dict) and isinstance(top.get("supports_vision"), bool):
        return top["supports_vision"]
    return None


def _default_supports_vision(
    provider: str, model: str, cfg: Optional[Dict[str, Any]] = None
) -> Optional[bool]:
    """默认：Spirit 无 models.dev 元数据源 → 恒 None（未知，交由 fail-closed 策略）。"""
    return None


def _default_accepts_tool_image(provider: str, model: str) -> Optional[bool]:
    """默认：无 provider 能力表 → 恒 None（未知，fail-closed 到辅助路由）。"""
    return None


def should_route_capture_to_aux_vision(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
    *,
    user_declared_lookup: Optional[UserDeclaredFn] = None,
    supports_vision_lookup: Optional[SupportsVisionFn] = None,
    accepts_tool_image_lookup: Optional[AcceptsToolImageFn] = None,
) -> bool:
    """当且仅当截图应经辅助视觉预分析时返回 True。

    Args:
        provider: 当前推理 provider id（小写规范 id，如 ``"openrouter"``）。
        model: 当前主模型 slug（发给 provider 的形式）。
        cfg: 已加载的 config dict（或 None）。
        user_declared_lookup: 用户声明 supports_vision 查询 seam（缺省读 cfg）。
        supports_vision_lookup: 元数据 supports_vision 查询 seam（缺省 None）。
        accepts_tool_image_lookup: provider 是否接受工具结果图片 seam（缺省 None）。

    Returns:
        True → 交给辅助视觉管线（对外只呈现纯文本工具结果）；
        False → 保留多模态信封（主模型原生处理视觉）。
    """
    if _explicit_aux_vision_override(cfg):
        return True

    user_lookup = user_declared_lookup or _default_user_declared
    supports_lookup = supports_vision_lookup or _default_supports_vision
    accepts_lookup = accepts_tool_image_lookup or _default_accepts_tool_image

    try:
        user_declared = user_lookup(cfg, provider, model)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("vision_routing: 用户声明查询失败: %s", exc)
        user_declared = None
    if user_declared is True:
        return False
    if user_declared is False:
        return True

    try:
        accepts_tool_image = accepts_lookup(provider, model)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("vision_routing: 工具结果图片支持查询失败: %s", exc)
        accepts_tool_image = None
    if accepts_tool_image is None or accepts_tool_image is False:
        return True

    try:
        supports_vision = supports_lookup(provider, model, cfg)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("vision_routing: supports_vision 查询失败: %s", exc)
        supports_vision = None
    if supports_vision is True:
        return False
    return True


__all__ = ["should_route_capture_to_aux_vision"]
