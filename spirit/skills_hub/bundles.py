"""技能捆绑包 — Spirit Agent（Phase 4.6）。

对标 Hermes ``agent/skill_bundles.py``：捆绑包是一个小 YAML 文件，命名一组要一起加载的
技能。从 CLI / 网关调用 ``/<bundle-name>`` 会把每个被引用技能的完整正文载进**同一条**
用户消息——和 ``/<skill-name>`` 一样，只是一次 N 个技能。

存储::

    <SPIRIT_HOME>/skill-bundles/*.yaml    （SPIRIT_BUNDLES_DIR 覆盖）

每个文件形如::

    name: backend-dev
    description: 后端功能开发——代码评审、测试、PR 流程。
    skills:
      - github-code-review
      - test-driven-development
    instruction: |
      注入到技能正文之上的可选额外指导。

文件 stem 作为 ``name:`` 缺失时的回退名，故往目录里丢一个 YAML 就注册了一个新捆绑包。

冲突解决：捆绑包与技能同 slug 时**捆绑包胜出**——slash 派发先查捆绑包，再回退技能。
这是预期行为：把捆绑包命名为 ``research`` 的用户明确想让 ``/research`` 指他的捆绑包。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spirit.skills_hub import paths
from spirit.skills_hub.commands import slugify

logger = logging.getLogger(__name__)

_bundles_cache: Dict[str, Dict[str, Any]] = {}
_bundles_cache_mtime: Optional[float] = None


def _iter_bundle_files() -> List[Path]:
    base = paths.bundles_dir()
    if not base.exists():
        return []
    files: List[Path] = []
    for ext in ("*.yaml", "*.yml"):
        files.extend(sorted(base.glob(ext)))
    return files


def _max_mtime(files: List[Path]) -> float:
    """捆绑文件 + 目录本身的最大 mtime（目录 mtime 捕捉删除，文件 mtime 捕捉编辑）。"""
    base = paths.bundles_dir()
    mtimes: List[float] = []
    if base.exists():
        try:
            mtimes.append(base.stat().st_mtime)
        except OSError:
            pass
    for f in files:
        try:
            mtimes.append(f.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _load_bundle_file(path: Path) -> Optional[Dict[str, Any]]:
    """解析单个捆绑 YAML（任何错误返回 None，不抛——坏捆绑不应拖垮 slash 发现）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("无法读取捆绑包 %s: %s", path, exc)
        return None
    try:
        import yaml
        data = yaml.safe_load(raw)
    except Exception as exc:
        logger.warning("捆绑包 %s YAML 非法: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("捆绑包 %s 不是映射；跳过", path)
        return None

    name = str(data.get("name") or path.stem).strip()
    if not name:
        logger.warning("捆绑包 %s 无名；跳过", path)
        return None

    skills = data.get("skills") or []
    if not isinstance(skills, list) or not skills:
        logger.warning("捆绑包 %s 无 skills 列表；跳过", path)
        return None
    skills = [str(s).strip() for s in skills if str(s).strip()]
    if not skills:
        logger.warning("捆绑包 %s skills 列表为空；跳过", path)
        return None

    description = str(data.get("description") or "").strip()
    instruction = str(data.get("instruction") or "").strip()

    slug = slugify(name)
    if not slug:
        logger.warning("捆绑包 %s 归一化为空 slug；跳过", path)
        return None

    return {
        "name": name,
        "slug": slug,
        "description": description or f"Load {len(skills)} skills as a bundle",
        "skills": skills,
        "instruction": instruction,
        "path": str(path),
    }


def scan_bundles() -> Dict[str, Dict[str, Any]]:
    """扫描捆绑目录并重建缓存，返回 ``{"/slug": bundle_info}``（重复 slug 首个胜出）。"""
    global _bundles_cache, _bundles_cache_mtime
    files = _iter_bundle_files()
    out: Dict[str, Dict[str, Any]] = {}
    for f in files:
        info = _load_bundle_file(f)
        if not info:
            continue
        key = f"/{info['slug']}"
        if key in out:
            logger.warning("重复捆绑 slug %s 来自 %s；保留 %s", key, f, out[key]["path"])
            continue
        out[key] = info
    _bundles_cache = out
    _bundles_cache_mtime = _max_mtime(files)
    return out


def get_skill_bundles() -> Dict[str, Dict[str, Any]]:
    """返回当前捆绑映射（磁盘变化时重扫；可反复廉价调用）。"""
    files = _iter_bundle_files()
    current_mtime = _max_mtime(files)
    if not _bundles_cache or _bundles_cache_mtime != current_mtime:
        scan_bundles()
    return _bundles_cache


def invalidate_bundles() -> None:
    """测试钩子 / 写操作后：清空捆绑缓存，强制下次重扫。"""
    global _bundles_cache, _bundles_cache_mtime
    _bundles_cache = {}
    _bundles_cache_mtime = None


def resolve_bundle_command_key(command: str) -> Optional[str]:
    """把用户输入的命令解析为规范捆绑 slash key（连字符 / 下划线互通）。"""
    if not command:
        return None
    cmd_key = f"/{command.replace('_', '-')}"
    return cmd_key if cmd_key in get_skill_bundles() else None


def reload_bundles() -> Dict[str, Any]:
    """重扫捆绑目录并返回差异（``added`` / ``removed`` / ``unchanged`` / ``total``）。"""
    def _snapshot(cmds: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        return {k.lstrip("/"): (v or {}).get("description", "") for k, v in cmds.items()}

    before = _snapshot(_bundles_cache)
    new = scan_bundles()
    after = _snapshot(new)

    added_names = sorted(set(after) - set(before))
    removed_names = sorted(set(before) - set(after))
    unchanged = sorted(set(after) & set(before))

    return {
        "added": [{"name": n, "description": after[n]} for n in added_names],
        "removed": [{"name": n, "description": before[n]} for n in removed_names],
        "unchanged": unchanged,
        "total": len(after),
    }


def list_bundles() -> List[Dict[str, Any]]:
    """返回按 slug 排序的捆绑信息列表（供展示）。"""
    return sorted(get_skill_bundles().values(), key=lambda b: b["slug"])


def build_bundle_invocation_message(
    cmd_key: str,
    user_instruction: str = "",
    task_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> Optional[Tuple[str, List[str], List[str]]]:
    """构建捆绑 slash 调用的用户消息，返回 ``(message, loaded_names, missing)``（找不到 None）。

    引用了未安装技能的捆绑仍会加载——agent 会收到哪些被跳过的提示（宽容立场，同
    ``build_preloaded_skills_prompt``）。禁用技能也被跳过：捆绑经 ``_load_skill_payload``
    直接载成员，绕过扫描期禁用过滤，故须在此重新施加禁用门。
    """
    bundles = get_skill_bundles()
    info = bundles.get(cmd_key)
    if not info:
        return None

    # 延迟导入避免包级循环（commands 已在模块级导入，这里取内部 helper）。
    from spirit.skills_hub import commands, discovery, usage

    try:
        disabled_names = discovery.get_disabled_skill_names(platform=platform)
    except Exception:
        disabled_names = set()

    loaded_names: List[str] = []
    missing: List[str] = []
    disabled: List[str] = []
    skill_blocks: List[str] = []
    seen: set = set()

    bundle_name = info["name"]
    extra_instruction = info.get("instruction") or ""

    for skill_id in info["skills"]:
        identifier = (skill_id or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        loaded = commands._load_skill_payload(identifier, task_id=task_id)
        if not loaded:
            missing.append(identifier)
            continue
        loaded_skill, skill_dir, skill_name = loaded

        if skill_name in disabled_names or identifier in disabled_names:
            disabled.append(skill_name or identifier)
            continue

        try:
            usage.bump_use(skill_name)
        except Exception:
            pass

        activation_note = f'[Loaded as part of the "{bundle_name}" skill bundle.]'
        skill_blocks.append(
            commands._build_skill_message(loaded_skill, skill_dir, activation_note, session_id=task_id)
        )
        loaded_names.append(skill_name)

    if not skill_blocks:
        return None

    header_lines = [
        f'[IMPORTANT: The user has invoked the "{bundle_name}" skill bundle, '
        f"loading {len(loaded_names)} skills together. Treat every skill below "
        "as active guidance for this turn.]",
        "",
        f"Bundle: {bundle_name}",
        f"Skills loaded: {', '.join(loaded_names)}",
    ]
    if missing:
        header_lines.append(f"Skills missing (skipped): {', '.join(missing)}")
    if disabled:
        header_lines.append(f"Skills disabled for this platform (skipped): {', '.join(disabled)}")
    if extra_instruction:
        header_lines.extend(["", f"Bundle instruction: {extra_instruction}"])
    if user_instruction:
        header_lines.extend(["", f"User instruction: {user_instruction}"])

    header = "\n".join(header_lines)
    return ("\n\n".join([header, *skill_blocks]), loaded_names, missing)


# ---------------------------------------------------------------------------
# 文件级 CRUD（供 /skill bundle 子命令 / CLI 消费）
# ---------------------------------------------------------------------------

def bundle_path_for(name: str) -> Path:
    """返回捆绑名的规范文件系统路径（slug 为空抛 ValueError）。"""
    slug = slugify(name)
    if not slug:
        raise ValueError(f"捆绑名 {name!r} 归一化为空 slug")
    return paths.bundles_dir() / f"{slug}.yaml"


def save_bundle(
    name: str,
    skills: List[str],
    description: str = "",
    instruction: str = "",
    overwrite: bool = False,
) -> Path:
    """写捆绑到磁盘并失效缓存（目标存在且 ``overwrite=False`` 抛 FileExistsError）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("捆绑名必填")
    cleaned_skills = [str(s).strip() for s in skills if str(s).strip()]
    if not cleaned_skills:
        raise ValueError("捆绑须至少引用一个技能")

    path = bundle_path_for(name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"捆绑已存在于 {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"name": name, "skills": cleaned_skills}
    if description:
        payload["description"] = description
    if instruction:
        payload["instruction"] = instruction

    import yaml
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    scan_bundles()
    return path


def delete_bundle(name: str) -> Path:
    """按名删除捆绑，返回被删路径（不存在抛 FileNotFoundError）。"""
    path = bundle_path_for(name)
    if not path.exists():
        raise FileNotFoundError(f"{path} 处无捆绑")
    path.unlink()
    scan_bundles()
    return path


def get_bundle(name: str) -> Optional[Dict[str, Any]]:
    """按名（slug 归一化）查捆绑。"""
    slug = slugify(name)
    return get_skill_bundles().get(f"/{slug}")


__all__ = [
    "scan_bundles",
    "get_skill_bundles",
    "invalidate_bundles",
    "resolve_bundle_command_key",
    "reload_bundles",
    "list_bundles",
    "build_bundle_invocation_message",
    "bundle_path_for",
    "save_bundle",
    "delete_bundle",
    "get_bundle",
]
