"""代理鉴权 —— Bearer token 校验（纯函数、常量时间比较）。

对标 Hermes api-server 计划的「Bearer token via Authorization header」，按 Spirit
精简为一个无状态纯函数，便于离线单测（不起服务、不读密钥文件）。

安全要点：
- 用 :func:`hmac.compare_digest` 做常量时间比较，避免计时侧信道。
- ``expected`` 为空/None 表示**不要求鉴权**（仅在 localhost 绑定时建议使用）。
"""

from __future__ import annotations

import hmac
from typing import Optional

_BEARER_PREFIX = "bearer "


def extract_bearer(header_value: Optional[str]) -> Optional[str]:
    """从 ``Authorization`` 头提取 token（不区分大小写的 ``Bearer`` 方案）。

    返回去掉前缀并 strip 的 token；无有效 Bearer 前缀时返回 None。
    """
    if not header_value:
        return None
    value = header_value.strip()
    if value.lower().startswith(_BEARER_PREFIX):
        token = value[len(_BEARER_PREFIX):].strip()
        return token or None
    return None


def verify_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """校验 token。

    - ``expected`` 为空 → 不要求鉴权，恒 True。
    - 否则要求 ``provided`` 与 ``expected`` 常量时间相等。
    """
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def is_authorized(auth_header: Optional[str], expected_key: Optional[str]) -> bool:
    """组合 :func:`extract_bearer` + :func:`verify_token` 的一步式判定。"""
    return verify_token(extract_bearer(auth_header), expected_key)


__all__ = ["extract_bearer", "verify_token", "is_authorized"]
