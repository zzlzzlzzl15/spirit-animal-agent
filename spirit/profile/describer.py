"""Profile 自动描述器 — Spirit Agent（Phase 4.2）。

对标 Hermes ``hermes_cli/profile_describer.py``（289 行）：读一个 profile 的
**技能名 + 模型/provider + 名字**，让辅助 LLM 产出 1–2 句"这个 profile 擅长什么"
的描述，写入 ``<profile_dir>/profile.yaml``（``description_auto: true``，便于
UI 标"待复核"）。用户事后可手改确认。

**关键架构适配**（对齐 ``spirit.goals.judge``）：Hermes 内部
``from agent.auxiliary_client import call_llm`` 直接调辅助模型；Spirit 没有
auxiliary_client，故 :func:`describe_profile` 改为接收**可注入的** ``llm_caller``
回调（签名 ``(messages, temperature, max_tokens, timeout) -> str``）。这样：

- ``describe_profile`` / ``_extract_json_blob`` 都是**纯函数**，测试传一个返回固定
  JSON 的假 caller 即可，无需真实 LLM。
- 生产环境由 :func:`spirit.goals.judge.build_agent_llm_caller` 从 ``SpiritAgent.client``
  构造 caller 注入（复用主对话的 OpenAI 兼容客户端发起一次 side call）。

设计要点（与 Hermes 一致）：
- **只读技能名，不读技能正文**——名字 + 分类已是足够的角色信号，且避免 100+ 技能的
  profile 撑爆上下文。
- **不读记忆**——记忆是个人隐私，编排器按"角色"而非"传记"派活。
- **绝不抛**：profile 缺失 / 无 caller / API 错误 / 畸形回复一律 ``ok=False``，
  以便一次批量扫描能越过单个失败继续。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from spirit.goals.judge import LLMCaller  # 复用同款注入签名
from spirit.profile import manager as _manager

logger = logging.getLogger(__name__)

# 喂给 LLM 的技能名上限。200+ 技能的 profile（少见但可能）否则会撑爆上下文。
MAX_SKILLS_FOR_PROMPT = 60

# 描述长度上限（字符），与 Hermes 保持一致。
_MAX_DESCRIPTION_CHARS = 280

_SYSTEM_PROMPT = """You are a profile-describer for the Spirit Agent.

A user runs multiple "profiles" — distinct agent identities, each with their
own skills, model, and configuration. An orchestrator routes work to whichever
profile best fits each task. To do that well, every profile needs a short,
concrete description of what it's good at.

You are given a profile's:
  - Name
  - Model / provider
  - List of installed skill names (a strong signal of role / domain)

Produce a single JSON object with exactly one key:

  {
    "description": "<1-2 sentence description, plain prose, no preamble>"
  }

Rules:
  - The description is what an orchestrator will read to decide whether to
    route a task here. Lead with the profile's strongest capability.
  - Stay concrete. Bad: "an AI agent that helps users."
                  Good: "Reads and modifies Python codebases — runs tests,
                         refactors functions, opens GitHub PRs."
  - 1-2 sentences, <= 280 characters total.
  - Never invent capabilities the skills don't suggest.
  - Never write "Spirit Agent profile" or other meta-narration.
  - No code fences, no preamble, no closing remarks. Output only JSON.
"""

_USER_TEMPLATE = """Profile name: {name}
Default model: {model}
Provider: {provider}
Installed skill count: {skill_count}
Notable skills (up to {skill_cap}):
{skill_list}
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DescribeOutcome:
    """描述单个 profile 的结果。``ok=False`` 承载所有预期失败，绝不抛。"""

    profile_name: str
    ok: bool
    reason: str = ""
    description: Optional[str] = None


def _collect_skills(profile_dir: Path) -> List[str]:
    """返回稳定、封顶的技能名列表，供 prompt 用。

    格式 ``category/skill_name``（category 是 ``skills/`` 下的直接子目录，如
    ``devops``）；直接位于 ``skills/`` 下的技能显示为裸 ``skill_name``。
    """
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return []
    try:
        from spirit.skills_hub.discovery import is_excluded_skill_path
    except Exception:  # pragma: no cover - 防御：discovery 不可用时退化为不排除
        def is_excluded_skill_path(_p):  # type: ignore
            return False
    names: List[str] = []
    for md in skills_dir.rglob("SKILL.md"):
        try:
            if is_excluded_skill_path(md):
                continue
            rel = md.relative_to(skills_dir)
        except (ValueError, OSError):
            continue
        parts = rel.parts[:-1]  # 去掉 SKILL.md 文件名
        if not parts:
            continue
        if len(parts) == 1:
            names.append(parts[0])
        else:
            names.append(f"{parts[0]}/{parts[-1]}")
    names.sort()
    # 封顶预算内。字母靠前的技能并不更重要——超过上限时**等距采样**而非取头部，
    # 免得一个 A..Z 全有的 profile 被描述成"以 A 开头"。
    if len(names) <= MAX_SKILLS_FOR_PROMPT:
        return names
    step = len(names) / MAX_SKILLS_FOR_PROMPT
    return [names[int(i * step)] for i in range(MAX_SKILLS_FOR_PROMPT)]


def _extract_json_blob(raw: str) -> Optional[dict]:
    """尽力从模型回复里抽出第一个 JSON 对象（剥围栏 + 首尾大括号定位）。"""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def describe_profile(
    profile_name: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    overwrite: bool = False,
    timeout: float = 60.0,
    max_tokens: int = 400,
) -> DescribeOutcome:
    """为一个 profile 自动生成描述。

    返回 :class:`DescribeOutcome`，对预期失败模式（profile 缺失、无 llm_caller、
    API 错误、畸形回复）一律 ``ok=False``，绝不抛——以便批量扫描越过单个失败。

    Args:
        profile_name: 目标 profile 名（``default`` 亦可，映射到根 HOME）。
        llm_caller: 可注入的辅助模型调用器（见模块 docstring）。为 None 时无法描述。
        overwrite: 是否覆盖已有的**用户手写**描述。默认拒绝覆盖 ``description_auto:
            false`` 的文本以保护用户策展；自动生成的（``description_auto: true``）
            始终可替换。
        timeout / max_tokens: 透传给 llm_caller 的调用参数。
    """
    canon = _manager.normalize_profile_name(profile_name)
    if not _manager.profile_exists(canon):
        return DescribeOutcome(canon, False, "profile not found")

    try:
        profile_dir = _manager.get_profile_dir(canon)
    except Exception as exc:  # pragma: no cover - 路径解析不该失败
        return DescribeOutcome(canon, False, f"cannot resolve profile dir: {exc}")

    # 保护用户策展描述，除非 --overwrite。
    existing = _manager.read_profile_meta(profile_dir)
    if existing.get("description") and not existing.get("description_auto") and not overwrite:
        return DescribeOutcome(
            canon,
            False,
            "profile already has a user-authored description (use overwrite to replace)",
        )

    if llm_caller is None:
        return DescribeOutcome(canon, False, "no llm_caller configured (auxiliary model unavailable)")

    skill_names = _collect_skills(profile_dir)
    skill_list = "\n".join(f"  - {n}" for n in skill_names) or "  (no skills installed)"
    try:
        model, provider = _manager._read_config_model(profile_dir)
    except Exception:
        model, provider = None, None

    user_msg = _USER_TEMPLATE.format(
        name=canon,
        model=(model or "(unset)"),
        provider=(provider or "(unset)"),
        skill_count=_manager._count_skills(profile_dir),
        skill_cap=MAX_SKILLS_FOR_PROMPT,
        skill_list=skill_list,
    )

    try:
        raw = llm_caller(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            0.3,  # temperature：低但仍留一点措辞变化
            max_tokens,
            timeout,
        ) or ""
    except Exception as exc:
        logger.info("describe: LLM 调用失败 %s (%s)", canon, exc)
        return DescribeOutcome(canon, False, f"LLM error: {type(exc).__name__}")

    parsed = _extract_json_blob(raw)
    if parsed is None:
        # 兜底：取原始文本首段。
        text = raw.strip().split("\n\n", 1)[0]
        if not text:
            return DescribeOutcome(canon, False, "LLM returned an empty response")
        description = text[:_MAX_DESCRIPTION_CHARS]
    else:
        val = parsed.get("description")
        if not isinstance(val, str) or not val.strip():
            return DescribeOutcome(canon, False, "LLM response missing 'description' field")
        description = val.strip()[:_MAX_DESCRIPTION_CHARS]

    try:
        _manager.write_profile_meta(profile_dir, description=description, description_auto=True)
    except Exception as exc:
        return DescribeOutcome(canon, False, f"failed to write profile.yaml: {exc}")

    return DescribeOutcome(canon, True, "described", description=description)


def list_describable_profiles(*, missing_only: bool = True) -> List[str]:
    """返回可被描述的 profile 名。

    ``missing_only=True``（默认）只返回尚无描述者；``False`` 返回全部。已有用户手写
    描述（``description_auto=false``）者在 ``missing_only`` 下被跳过。
    """
    out: List[str] = []
    for p in _manager.list_profiles():
        if missing_only and (p.description or "").strip() and not p.description_auto:
            continue
        out.append(p.name)
    return out


__all__ = [
    "DescribeOutcome",
    "describe_profile",
    "list_describable_profiles",
    "MAX_SKILLS_FOR_PROMPT",
    # 供测试复用的内部件
    "_collect_skills",
    "_extract_json_blob",
]
