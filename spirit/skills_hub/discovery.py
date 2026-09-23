"""技能发现与元数据匹配 — Spirit Agent（Phase 4.6）。

对标 Hermes ``agent/skill_utils.py``（frontmatter 解析 + 平台/环境匹配 + 禁用列表 +
外部技能目录 + 索引迭代）与 ``tools/skills_tool.py`` 的发现层。这是 Spirit 技能发现的
**单一事实源**：``spirit.tools.skills`` 的 list/view/scan 工具、``commands`` 的 slash
派发、``bundles`` 的成员加载都经此模块，避免多处各写一套发现逻辑而漂移。

设计要点（对齐 Hermes）：

- **平台门控**（``platforms:``）是硬兼容门：不匹配当前 OS 的技能在 offer 面（索引 /
  slash 列表）被隐藏。缺省 = 兼容所有平台。
- **环境门控**（``environments:``）是相关性门而非兼容门：仅在 offer 面过滤，显式加载
  （``skill_view`` / 预加载）永远放行；未知环境标签 fail-open（绝不因看不懂的标签藏技能）。
- **禁用列表**（``skills.disabled`` / ``skills.platform_disabled``）来自配置，全局禁用 ∪
  平台禁用。
- **外部目录**（``skills.external_dirs``）：本地 ``<home>/skills`` 优先，其后按配置顺序。

本模块**刻意不导入**工具注册表或任何重依赖链，可安全地在模块级导入。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from spirit.skills_hub import paths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置读取（seam：测试可 monkeypatch load_skills_config）
# ---------------------------------------------------------------------------

def load_skills_config() -> Dict[str, Any]:
    """读取 ``config.yaml`` 的 ``skills`` 节（尽力而为，异常降级为空 dict）。"""
    try:
        from spirit.config import load_config

        cfg = load_config() or {}
        section = cfg.get("skills")
        if isinstance(section, dict):
            return section
    except Exception:
        logger.debug("无法读取 skills 配置", exc_info=True)
    return {}


# ---------------------------------------------------------------------------
# YAML / frontmatter
# ---------------------------------------------------------------------------

_yaml_load_fn = None


def yaml_load(content: str):
    """惰性导入 YAML，优先 CSafeLoader（对标 Hermes ``skill_utils.yaml_load``）。"""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """从 markdown 解析 YAML frontmatter，返回 ``(frontmatter_dict, body)``。

    - 剥离单个前导 UTF-8 BOM（Windows 记事本 / PowerShell ``>`` 保存 UTF-8 时会加，
      ``read_text(encoding="utf-8")`` 不会剥，留着会让 ``---`` 栅栏检查失败而静默丢弃
      整个 frontmatter）。
    - YAML 解析失败时回退到简单 ``key: value`` 逐行切分（健壮性）。
    """
    frontmatter: Dict[str, Any] = {}
    if content.startswith("\ufeff"):
        content = content[1:]
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]

    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ---------------------------------------------------------------------------
# 路径排除 / 支持区判定
# ---------------------------------------------------------------------------

def is_skill_support_path(path) -> bool:
    """``path`` 是否位于某个真实技能根的支持区（references/templates/assets/scripts）。

    支持区是渐进式披露数据，经 ``skill_view(file=...)`` 显式加载，不是独立技能的发现根。
    只有当支持目录**直接**位于含 ``SKILL.md`` 的目录下时才算支持区，故 ``skills/scripts/foo``
    这类合法分类 / 技能名仍可被发现。
    """
    path_obj = path if isinstance(path, Path) else Path(str(path))
    parts = path_obj.parts
    for idx, part in enumerate(parts[:-1]):
        if part not in paths.SKILL_SUPPORT_DIRS or idx == 0:
            continue
        skill_root = Path(*parts[:idx])
        if (skill_root / "SKILL.md").exists():
            return True
    return False


def is_excluded_skill_path(path) -> bool:
    """``path`` 是否应被活动技能扫描器跳过（依赖 / venv / VCS / 缓存 / 支持区）。"""
    try:
        parts = path.parts
    except AttributeError:
        from pathlib import PurePath
        parts = PurePath(str(path)).parts
    return any(part in paths.EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(path)


def iter_skill_index_files(skills_dir: Path, filename: str):
    """遍历 ``skills_dir``，产出排序后的、匹配 ``filename`` 的路径。

    排除元数据 / VCS / venv / 缓存 / 支持目录（对标 Hermes ``iter_skill_index_files``）。
    """
    matches: List[str] = []
    for root, dirs, files in os.walk(str(skills_dir), followlinks=True):
        has_skill_md = "SKILL.md" in files
        dirs[:] = [
            d for d in dirs
            if d not in paths.EXCLUDED_SKILL_DIRS
            and not (has_skill_md and d in paths.SKILL_SUPPORT_DIRS)
        ]
        if filename in files:
            matches.append(os.path.join(root, filename))
    for p in sorted(matches):
        yield Path(p)


# ---------------------------------------------------------------------------
# 平台匹配
# ---------------------------------------------------------------------------

PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


def skill_matches_platform_list(platforms: Any) -> bool:
    """``platforms`` 是否与当前 OS 兼容（缺省 / 空 = 兼容所有）。"""
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """技能是否与当前 OS 兼容（读 frontmatter 的 ``platforms`` 列表）。"""
    return skill_matches_platform_list(frontmatter.get("platforms"))


# ---------------------------------------------------------------------------
# 环境匹配（相关性门，非兼容门；offer 时过滤，显式加载放行）
# ---------------------------------------------------------------------------

_KNOWN_ENVIRONMENTS = frozenset({"docker"})
_ENV_DETECT_CACHE: Dict[str, bool] = {}


def _is_container() -> bool:
    """粗判是否运行在容器内（``/.dockerenv`` 或 cgroup 标记）。"""
    try:
        if Path("/.dockerenv").exists():
            return True
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            text = cgroup.read_text(encoding="utf-8", errors="replace")
            return "docker" in text or "kubepods" in text or "containerd" in text
    except OSError:
        pass
    return False


def _detect_environment(env: str) -> bool:
    """命名运行时环境当前是否活跃（按进程缓存；未知环境 fail-open 返回 True）。"""
    if env in _ENV_DETECT_CACHE:
        return _ENV_DETECT_CACHE[env]
    result = True
    if env == "docker":
        result = _is_container()
    _ENV_DETECT_CACHE[env] = result
    return result


def clear_env_cache() -> None:
    """测试钩子：清空环境探测缓存。"""
    _ENV_DETECT_CACHE.clear()


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """技能是否与当前运行时环境相关（读 frontmatter 的 ``environments`` 列表）。

    OR 语义：任一声明环境活跃即匹配。缺省 / 空 = 所有环境相关。未知标签 fail-open。
    这是 offer 时过滤，**不**被 ``skill_view`` / 预加载强制——显式加载即显式同意。
    """
    environments = frontmatter.get("environments")
    if not environments:
        return True
    if not isinstance(environments, list):
        environments = [environments]
    for env in environments:
        normalized = str(env).lower().strip()
        if not normalized:
            continue
        if normalized not in _KNOWN_ENVIRONMENTS:
            return True
        if _detect_environment(normalized):
            return True
    return False


# ---------------------------------------------------------------------------
# 禁用列表
# ---------------------------------------------------------------------------

def _normalize_string_set(values) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


def get_disabled_skill_names(platform: Optional[str] = None) -> Set[str]:
    """从配置读禁用技能名（全局 ``skills.disabled`` ∪ 平台 ``skills.platform_disabled``）。

    ``platform`` 为 None 时从 ``SPIRIT_PLATFORM`` 环境变量解析。全局禁用的技能在每个
    平台都保持禁用。
    """
    skills_cfg = load_skills_config()
    if not skills_cfg:
        return set()
    resolved_platform = platform or os.getenv("SPIRIT_PLATFORM")
    global_disabled = _normalize_string_set(skills_cfg.get("disabled"))
    if resolved_platform:
        platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(resolved_platform)
        if platform_disabled is not None:
            return global_disabled | _normalize_string_set(platform_disabled)
    return global_disabled


# ---------------------------------------------------------------------------
# 外部技能目录
# ---------------------------------------------------------------------------

def get_external_skills_dirs() -> List[Path]:
    """读 ``skills.external_dirs`` 并返回校验后的绝对路径（仅存在的目录）。

    每项展开 ``~`` 与 ``${VAR}``；相对路径相对 ``SPIRIT_HOME`` 解析（而非 cwd）；
    去重并跳过等于本地 ``<home>/skills`` 的项。
    """
    skills_cfg = load_skills_config()
    if not skills_cfg:
        return []
    raw_dirs = skills_cfg.get("external_dirs")
    if not raw_dirs:
        return []
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        return []

    home = paths.spirit_home()
    local_skills = paths.skills_dir().resolve()
    seen: Set[Path] = set()
    result: List[Path] = []
    for entry in raw_dirs:
        entry = str(entry).strip()
        if not entry:
            continue
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        p = (home / p).resolve() if not p.is_absolute() else p.resolve()
        if p == local_skills or p in seen:
            continue
        if p.is_dir():
            seen.add(p)
            result.append(p)
        else:
            logger.debug("外部技能目录不存在，跳过: %s", p)
    return result


def get_all_skills_dirs() -> List[Path]:
    """所有技能目录：本地 ``<home>/skills`` 优先，其后按配置顺序的外部目录。"""
    dirs = [paths.skills_dir()]
    dirs.extend(get_external_skills_dirs())
    return dirs


def normalize_skill_lookup_name(identifier: str) -> str:
    """把技能标识符归一化为 ``skill_view`` 安全的相对路径。

    slash 命令 / 定时任务可能存了 ``<home>/skills`` 下技能的绝对路径；``skill_view``
    出于安全拒绝绝对名，故调用方先把受信绝对路径翻译成相对形式。非绝对路径原样返回
    （仅剥前导 ``/``）。
    """
    raw = (identifier or "").strip()
    if not raw:
        return raw
    ident_path = Path(raw).expanduser()
    if not ident_path.is_absolute():
        return raw.lstrip("/")

    trusted_roots = [paths.skills_dir()]
    try:
        trusted_roots.extend(get_external_skills_dirs())
    except Exception:
        pass
    for root in trusted_roots:
        try:
            return str(ident_path.relative_to(root))
        except ValueError:
            continue
    try:
        return str(ident_path.resolve().relative_to(paths.skills_dir().resolve()))
    except Exception:
        logger.debug("技能标识符 %r 是受信根之外的绝对路径——原样透传", raw)
        return raw


# ---------------------------------------------------------------------------
# SkillMeta + 全量发现
# ---------------------------------------------------------------------------

@dataclass
class SkillMeta:
    """技能元数据（列表 / 视图 / slash 派发的公共形状）。"""

    name: str
    description: str
    version: str = ""
    path: str = ""
    tags: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    environments: List[str] = field(default_factory=list)


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def parse_skill_md(skill_md_path: Path) -> Optional[SkillMeta]:
    """解析 ``SKILL.md`` 的 frontmatter 为 :class:`SkillMeta`（无 name/desc 返回 None）。"""
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm, _body = parse_frontmatter(text)
    if not fm:
        return None

    name = str(fm.get("name", "")).strip()
    desc = str(fm.get("description", "")).strip()
    if not name or not desc:
        return None
    if len(name) > 64:
        name = name[:64]
    if len(desc) > 1024:
        desc = desc[:1024]

    meta = fm.get("metadata", {})
    spirit_meta = meta.get("spirit", {}) if isinstance(meta, dict) else {}
    tags = spirit_meta.get("tags", []) if isinstance(spirit_meta, dict) else []

    return SkillMeta(
        name=name,
        description=desc,
        version=str(fm.get("version", "")),
        path=str(skill_md_path.parent),
        tags=_as_str_list(tags),
        platforms=_as_str_list(fm.get("platforms")),
        environments=_as_str_list(fm.get("environments")),
    )


def find_all_skills(
    *,
    apply_gates: bool = False,
    platform: Optional[str] = None,
) -> List[SkillMeta]:
    """扫描所有技能目录，返回按名排序的 :class:`SkillMeta` 列表。

    ``apply_gates=True`` 时额外施加平台 / 环境 / 禁用门（offer 面用）；默认 False
    返回全部已解析技能（``skills_list`` 工具用，门控在工具层按需施加）。
    """
    disabled = get_disabled_skill_names(platform=platform) if apply_gates else set()
    seen_names: Set[str] = set()
    skills: List[SkillMeta] = []

    for scan_dir in get_all_skills_dirs():
        if not scan_dir.is_dir():
            continue
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            try:
                fm, _body = parse_frontmatter(
                    skill_md.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
            name = str(fm.get("name") or skill_md.parent.name)
            if name in seen_names:
                continue
            if apply_gates:
                if not skill_matches_platform(fm):
                    continue
                if not skill_matches_environment(fm):
                    continue
                if name in disabled:
                    continue
            meta = parse_skill_md(skill_md)
            if meta is None:
                continue
            seen_names.add(name)
            skills.append(meta)

    return sorted(skills, key=lambda s: s.name.lower())


__all__ = [
    "load_skills_config",
    "yaml_load",
    "parse_frontmatter",
    "is_skill_support_path",
    "is_excluded_skill_path",
    "iter_skill_index_files",
    "PLATFORM_MAP",
    "skill_matches_platform_list",
    "skill_matches_platform",
    "skill_matches_environment",
    "clear_env_cache",
    "get_disabled_skill_names",
    "get_external_skills_dirs",
    "get_all_skills_dirs",
    "normalize_skill_lookup_name",
    "SkillMeta",
    "parse_skill_md",
    "find_all_skills",
]
