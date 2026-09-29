"""tests/profile 共享 fixture。

隔离策略对标 ``tests/moa/conftest.py`` 的 ``moa_home``：``spirit.profile.paths`` 全部
**按调用**读模块全局 ``spirit.config.SPIRIT_HOME``，故 monkeypatch 该全局后，所有下游
路径（default_root/profiles_root/profile_dir/active_profile_path）立即指向 tmp_path，
测试之间零污染、可并行。

``FakeCaller`` 复用 ``tests/goals/conftest.py`` 的思路——describer 的 ``llm_caller``
与 judge 同款签名 ``(messages, temperature, max_tokens, timeout) -> str``，用假 caller
代替真实辅助模型，使 describe_profile 可离线单测。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """把 SPIRIT_HOME 隔离到 tmp_path（对标 Hermes HERMES_HOME / moa_home）。

    ``default`` profile 即 ``tmp_path`` 本身；命名 profile 落在 ``tmp_path/profiles/<name>``。
    """
    import spirit.config as config

    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    return tmp_path


class FakeCaller:
    """可注入的假 ``llm_caller``，等价于 Hermes 测试里 patch 的 ``call_llm``。

    签名与 :data:`spirit.goals.judge.LLMCaller` 一致：
    ``(messages, temperature, max_tokens, timeout) -> str``。

    - ``content`` 为字符串 → 每次都返回它。
    - ``content`` 为 Exception 实例 → 抛出它（模拟 API/传输错误，验证优雅降级）。
    - ``content`` 为可调用 → 每次调用它取返回值。

    每次调用的入参记录在 ``self.calls``，``last_user_msg`` / ``last_system_msg``
    便于断言"发给 describer 的 prompt 里带了技能名/模型/provider"。
    """

    def __init__(self, content: str = '{"description": "A test profile."}'):
        self.content = content
        self.calls: List[dict] = []

    def __call__(self, messages, temperature, max_tokens, timeout) -> str:
        self.calls.append({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        })
        content = self.content
        if isinstance(content, Exception):
            raise content
        if callable(content):
            return content()
        return content

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self):
        return self.calls[-1]["messages"] if self.calls else []

    @property
    def last_user_msg(self) -> str:
        return next((m["content"] for m in self.last_messages if m.get("role") == "user"), "")

    @property
    def last_system_msg(self) -> str:
        return next((m["content"] for m in self.last_messages if m.get("role") == "system"), "")


# ---------------------------------------------------------------------------
# 造数据辅助
# ---------------------------------------------------------------------------

def write_config_yaml(profile_dir: Path, model: str, provider: str) -> None:
    """在 profile 目录写一个最小 config.yaml（``llm`` 节含 model/provider）。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text(
        f"llm:\n  model: {model}\n  provider: {provider}\n",
        encoding="utf-8",
    )


def add_skill(profile_dir: Path, category: str, skill_name: str) -> None:
    """在 profile 的 ``skills/<category>/<skill_name>/SKILL.md`` 放一个技能。"""
    d = profile_dir / "skills" / category / skill_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {skill_name}\n\ntest skill\n", encoding="utf-8")


def add_flat_skill(profile_dir: Path, skill_name: str) -> None:
    """在 profile 的 ``skills/<skill_name>/SKILL.md`` 放一个无分类技能。"""
    d = profile_dir / "skills" / skill_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {skill_name}\n", encoding="utf-8")
