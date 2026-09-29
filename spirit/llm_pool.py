"""多 provider LLM 故障转移池 —— "哪个有 token 用哪个"。

配置来源：``~/.spirit/config.yaml`` 的 ``llm.failover``::

    llm:
      failover:
        enabled: true
        providers:            # 优先级顺序，探测到第一个健康的就用它
          - {name: qwen, provider: openai, model: qwen3.8-max,
             api_key: sk-xxx, base_url: https://.../compatible-mode/v1}
          - {name: minimax, provider: minimax, model: MiniMax-M3,
             api_key: sk-yyy, base_url: https://api.minimaxi.com/v1}

设计要点：
- **探测式**：``probe()`` 发一个真实小请求，去 think 块后正文非空才算"有 token/健康"
  （断供表现为连接错误 / 4xx-5xx / 空正文 / 余额不足）。
- **缓存**：``pick_provider()`` 结果缓存 ``CACHE_TTL`` 秒，避免热路径反复探测；
  批量场景每实例是新子进程，天然每实例重探一次（自适应 token 变化）。
- **非致命**：任何异常都回退到"单 provider"或列表首个，绝不因探测本身阻断调用方。

只依赖标准库 + PyYAML，不 import spirit 包内其他模块 → Spirit / Hermes / 批量三方均可安全引用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 探测结果缓存（进程内）：避免热路径每次 load_config 都发网络请求
_CACHE: Dict[str, Any] = {"picked": None, "ts": 0.0}
CACHE_TTL = 300.0  # 秒

_THINK_RE = re.compile(r"<think>.*?</tool_response>", re.S)


def _config_path() -> Path:
    return Path(os.environ.get("SPIRIT_HOME", "~/.spirit")).expanduser() / "config.yaml"


def _resolve_profile(name: Any) -> Any:
    """按 name/provider 从注册表取 ProviderProfile；不可用/无匹配返回 None。"""
    if not name:
        return None
    try:
        from spirit.providers import get_provider_profile
        return get_provider_profile(str(name).strip().lower())
    except Exception:  # noqa: BLE001 - 注册表不可用绝不阻断
        return None


def _key_from_env(profile: Any) -> str:
    """按 profile.env_vars 顺序取首个非空环境变量值；无 profile/无命中返回空串。"""
    if not profile:
        return ""
    for env_name in getattr(profile, "env_vars", ()) or ():
        val = os.environ.get(env_name, "")
        if val:
            return val
    return ""


def load_providers() -> List[Dict[str, Any]]:
    """读取 failover provider 列表；未配置则回退到 llm 顶层单 provider。

    每条目的 base_url/api_key 支持**声明式解析**：inline 值优先，缺省时从
    ProviderProfile 注册表（spirit.providers）按 provider/name 补 base_url，并按
    profile.env_vars 从环境变量补 api_key。返回的每个 dict 至少含
    name/provider/model/api_key/base_url。
    """
    try:
        import yaml
        cfg = yaml.safe_load(_config_path().read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 探测失败绝不阻断
        logger.debug("[llm-pool] 读取配置失败: %s", exc)
        return []

    llm = cfg.get("llm", {}) or {}
    fo = llm.get("failover", {}) or {}
    raw = fo.get("providers") or []

    providers: List[Dict[str, Any]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        prov_name = (p.get("provider") or "").strip()
        # 声明式解析：inline 值优先，缺省时从 ProviderProfile 注册表补 base_url/api_key
        profile = _resolve_profile(prov_name) or _resolve_profile(p.get("name"))
        base_url = p.get("base_url") or (profile.base_url if profile else "")
        api_key = p.get("api_key") or _key_from_env(profile)
        if not (base_url and api_key):
            continue  # inline 与 profile 都凑不齐 base_url+api_key，无法探测，跳过
        providers.append({
            "name": p.get("name") or prov_name or "provider",
            "provider": prov_name or "openai",
            "model": p.get("model") or "",
            "api_key": api_key,
            "base_url": base_url,
        })

    if providers:
        return providers

    # 回退：无 failover 列表时用 llm 顶层单 provider
    if llm.get("base_url") and llm.get("api_key"):
        return [{
            "name": llm.get("provider") or "default",
            "provider": llm.get("provider") or "auto",
            "model": llm.get("model") or "",
            "api_key": llm.get("api_key"),
            "base_url": llm.get("base_url"),
        }]
    return []


def failover_enabled() -> bool:
    """是否启用 failover（llm.failover.enabled）。默认 False → 不改变单 provider 行为。"""
    try:
        import yaml
        cfg = yaml.safe_load(_config_path().read_text(encoding="utf-8")) or {}
        return bool(((cfg.get("llm", {}) or {}).get("failover", {}) or {}).get("enabled", False))
    except Exception:  # noqa: BLE001
        return False


def probe(p: Dict[str, Any], timeout: int = 30) -> bool:
    """真实小请求探活：去 think 块后正文非空才算健康（= 有可用 token）。

    max_tokens 给足（2000），避免思考模型的 think 块吃光预算导致假阴性。
    """
    import urllib.request as _u

    url = (p.get("base_url") or "").rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": p.get("model"),
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        "max_tokens": 2000,
    }).encode("utf-8")
    req = _u.Request(url, data=body, headers={
        "Authorization": "Bearer " + (p.get("api_key") or ""),
        "Content-Type": "application/json",
    })
    try:
        resp = _u.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        msg = (data["choices"][0]["message"].get("content") or "")
        txt = _THINK_RE.sub("", msg).strip()
        # Qwen3 思考模型可能把推理放 reasoning_content，content 仍应有答案
        return resp.status == 200 and len(txt) > 0
    except Exception as exc:  # noqa: BLE001 - 任何错误都视为不健康
        logger.debug("[llm-pool] probe %s 失败: %s", p.get("name"), exc)
        return False


def pick_provider(force: bool = False, ttl: float = CACHE_TTL) -> Optional[Dict[str, Any]]:
    """按优先级探测，返回第一个"有 token/健康"的 provider dict。

    - 命中缓存（ttl 内）直接返回，不重复探测。
    - 全部不健康 → 返回列表首个（让上层照常报错/重试），且**不写缓存**（下次立即重探）。
    - 无配置 → 返回 None（调用方回退到自己的默认逻辑）。
    """
    providers = load_providers()
    if not providers:
        return None

    now = time.time()
    if not force and _CACHE["picked"] is not None and (now - _CACHE["ts"]) < ttl:
        return _CACHE["picked"]

    for p in providers:
        if probe(p):
            _CACHE["picked"] = p
            _CACHE["ts"] = now
            logger.info("[llm-pool] 选中 provider=%s model=%s", p.get("name"), p.get("model"))
            return p

    logger.warning("[llm-pool] 所有 provider 探活失败，回退首个 %s（不缓存）", providers[0].get("name"))
    return providers[0]


def invalidate_cache() -> None:
    """清空探测缓存（下次 pick_provider 强制重探）。调用失败切换时用。"""
    _CACHE["picked"] = None
    _CACHE["ts"] = 0.0


def any_healthy(timeout: int = 30) -> bool:
    """任一 provider 探活成功即返回 True。

    批量闸门语义：只有当**所有** provider 都断供（如全部额度耗尽）才判定不健康、
    触发等待；只要有一个“有 token”就放行。按优先级顺序探测，命中即止。
    无配置时返回 True（不拦截，交给 runner 自己报错）。
    """
    providers = load_providers()
    if not providers:
        return True
    for p in providers:
        if probe(p, timeout=timeout):
            return True
    return False


__all__ = [
    "load_providers",
    "failover_enabled",
    "probe",
    "pick_provider",
    "any_healthy",
    "invalidate_cache",
    "CACHE_TTL",
]
