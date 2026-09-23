"""tests/skills_hub 共享 fixture 与辅助函数。

对标 Hermes 各技能测试的隔离套路（``HERMES_HOME`` + ``monkeypatch.setenv`` + 重载模块 +
清缓存），适配 Spirit 技能中心的**按调用解析**路径设计：

- ``paths.spirit_home()`` 延迟导入并在**调用时**读 ``spirit.config.SPIRIT_HOME`` 模块全局，
  故 ``monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path)`` 立即反映到所有下游路径函数
  （skills_dir / hub_dir / bundles_dir / lock_file / audit_log / ...）。
- ``config.load_config()`` 无缓存、``get_config_path()`` 调用时查 ``SPIRIT_HOME`` 全局，故
  写 ``tmp_path/config.yaml`` 即被真实配置路径读到（``skills`` 节经 ``_deep_merge`` 覆盖
  ``DEFAULT_CONFIG`` 的同名节，其余默认值保留）。
- 每个测试都清掉模块级缓存（commands / bundles 扫描缓存、discovery 环境探测缓存）与市场
  fetch seam（hub 的 ``_DEFAULT_FETCHER`` / ``_FETCHERS`` / ``_INDEX_FETCHERS``），并删掉
  可能泄漏的路径 / 平台覆盖环境变量，保证测试间零串扰。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from spirit.skills_hub import bundles, commands, discovery, hub, paths


# ---------------------------------------------------------------------------
# 核心隔离 fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """把 ``SPIRIT_HOME`` 隔离到 ``tmp_path``，清所有缓存 / seam / 覆盖 env，建 ``skills/``。

    返回 ``tmp_path``（即 SPIRIT_HOME）。技能根为 ``tmp_path/skills``，捆绑根为
    ``tmp_path/skill-bundles``，市场元数据根为 ``tmp_path/skills/.hub``。
    """
    import spirit.config as config

    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)

    # 删掉会覆盖按调用解析路径 / 平台作用域的环境变量，保证测试从中性状态开始。
    for var in (
        "SPIRIT_SKILLS_DIR", "SPIRIT_HUB_DIR", "SPIRIT_BUNDLES_DIR",
        "SPIRIT_PLATFORM", "SPIRIT_HOME",
    ):
        monkeypatch.delenv(var, raising=False)

    # 清模块级扫描 / 探测缓存。
    commands.invalidate_skill_commands()
    bundles.invalidate_bundles()
    discovery.clear_env_cache()

    # 重置市场 fetch / index seam（进程级全局，setup 时清即可保证隔离）。
    hub.set_fetcher(None)
    hub._FETCHERS.clear()
    hub._INDEX_FETCHERS.clear()

    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 技能 / 捆绑工厂
# ---------------------------------------------------------------------------

@pytest.fixture
def make_skill(skills_home):
    """工厂 fixture：在技能根下创建一个带 ``SKILL.md`` 的技能目录，返回其路径。

    签名::

        make_skill(name, *, body="Do the thing.", description=None,
                   frontmatter_extra="", category=None, supporting=None)

    - ``description``：frontmatter 描述（缺省 ``"Description for <name>"``；传 ``""`` 省略）。
    - ``frontmatter_extra``：追加到 frontmatter 的原始 YAML 行（如 ``"platforms: [linux]"``）。
    - ``category``：把技能放进 ``skills/<category>/<name>``（测分类子目录发现）。
    - ``supporting``：``{相对路径: 内容}``，创建支持区文件（references/scripts/...）。

    每次创建后失效技能命令扫描缓存，故随后的 ``scan`` 一定看到新技能。
    """
    def _make(
        name: str,
        *,
        body: str = "Do the thing.",
        description: Optional[str] = None,
        frontmatter_extra: str = "",
        category: Optional[str] = None,
        supporting: Optional[Dict[str, str]] = None,
    ):
        skills_dir = paths.skills_dir()
        base = (skills_dir / category) if category else skills_dir
        skill_dir = base / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        desc = f"Description for {name}" if description is None else description
        fm_lines = [f"name: {name}"]
        if desc:
            fm_lines.append(f"description: {desc}")
        if frontmatter_extra:
            fm_lines.append(frontmatter_extra.strip("\n"))

        text = (
            "---\n" + "\n".join(fm_lines) + "\n---\n\n"
            f"# {name}\n\n{body}\n"
        )
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")

        for rel, content in (supporting or {}).items():
            fpath = skill_dir / rel
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

        commands.invalidate_skill_commands()
        return skill_dir

    return _make


@pytest.fixture
def make_bundle(skills_home):
    """工厂 fixture：在捆绑根下写一个捆绑 YAML，返回其路径。

    签名::

        make_bundle(slug, skills, *, description="", instruction="", name=None)

    ``slug`` 决定文件名（``<slug>.yaml``）；``name`` 缺省取 ``slug``。写后失效捆绑缓存。
    """
    def _make(
        slug: str,
        skills: List[str],
        *,
        description: str = "",
        instruction: str = "",
        name: Optional[str] = None,
    ):
        bdir = paths.bundles_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        lines = [f"name: {name or slug}"]
        if description:
            lines.append(f"description: {description}")
        lines.append("skills:")
        for s in skills:
            lines.append(f"  - {s}")
        if instruction:
            lines.append("instruction: |")
            for ln in instruction.splitlines():
                lines.append(f"  {ln}")
        path = bdir / f"{slug}.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bundles.invalidate_bundles()
        return path

    return _make


# ---------------------------------------------------------------------------
# 配置写入 helper（真实 config.yaml 路径）
# ---------------------------------------------------------------------------

def write_skills_config(home, **skills_section: Any) -> None:
    """把 ``skills`` 配置节写到 ``home/config.yaml``（被 ``load_config()`` 深合并到默认之上）。

    例::

        write_skills_config(home, disabled=["foo"], inline_shell=True)

    只覆盖给出的键；``DEFAULT_CONFIG.skills`` 的其余键（如 ``template_vars=True``）保留。
    """
    import yaml

    payload: Dict[str, Any] = {"skills": dict(skills_section)}
    (home / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 配置变化后失效受配置影响的扫描缓存。
    commands.invalidate_skill_commands()
    bundles.invalidate_bundles()


# ---------------------------------------------------------------------------
# 市场 bundle 工厂（离线 fetch seam 用）
# ---------------------------------------------------------------------------

def make_market_bundle(
    name: str,
    *,
    files: Optional[Dict[str, str]] = None,
    source: str = "community",
    identifier: str = "",
    trust_level: str = "community",
    metadata: Optional[Dict[str, Any]] = None,
) -> hub.SkillBundle:
    """构造一个内存 :class:`hub.SkillBundle`（默认含一个安全 ``SKILL.md``）。"""
    if files is None:
        files = {
            "SKILL.md": (
                f"---\nname: {name}\ndescription: Market skill {name}\n---\n\n"
                f"# {name}\n\nDo the market thing.\n"
            ),
        }
    return hub.SkillBundle(
        name=name,
        files=files,
        source=source,
        identifier=identifier or name,
        trust_level=trust_level,
        metadata=metadata or {},
    )
