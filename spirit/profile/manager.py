"""Profile 管理器 — Spirit Agent（Phase 4.2）。

对标 Hermes ``hermes_cli/profiles.py``（2225 行）的**核心子集**：多实例隔离 HOME 的
创建 / 列举 / 切换 / 删除 / 重命名 + profile.yaml 元数据 + 活跃 profile 粘性状态。

按 Spirit「精简子集 + 可测试」约定（对齐 ``computer_use`` / ``tui``），**不移植**与
Hermes 基础设施强耦合的部分：wrapper 别名脚本（对 Windows 不友好）、gateway service
注册（s6/systemd/launchd）、Git 分发（见 ``profile_distribution``，独立关注点，缓建）、
export/import 归档。这些在 ``docs/07-development-checklist.md`` Phase 4.2 备注为缓建。

路径解析全部委托 :mod:`spirit.profile.paths`（按调用读 ``config.SPIRIT_HOME``，
测试可 monkeypatch）。所有函数对「无关 profile 的损坏」保持容错——一个坏 profile.yaml
绝不该拖垮 ``list_profiles``。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from spirit.profile import paths as _paths

logger = logging.getLogger(__name__)

__all__ = [
    "ProfileInfo",
    "normalize_profile_name",
    "validate_profile_name",
    "get_profile_dir",
    "profile_exists",
    "read_profile_meta",
    "write_profile_meta",
    "list_profiles",
    "create_profile",
    "delete_profile",
    "rename_profile",
    "get_active_profile",
    "set_active_profile",
    "get_active_profile_name",
]

# 合法 profile id：小写字母数字开头，可含 -/_，最长 64。禁止 / . .. 等路径分隔与穿越。
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 保留名：与 Spirit 安装本身或常见系统命令冲突，拒绝作为 profile 名。
_RESERVED_NAMES = frozenset({"spirit", "test", "tmp", "root", "sudo", "default", "profiles"})

# 每个新 profile 引导创建的子目录（Spirit 风味的 HOME 布局）。
_PROFILE_DIRS = [
    "memories",
    "sessions",
    "skills",
    "skill-bundles",
    "logs",
    "plans",
    "workspace",
]

# --clone-config 时从源 profile 拷贝的文件（存在才拷）。
_CLONE_CONFIG_FILES = ["config.yaml", ".env", "SOUL.md"]

# --clone-config 时拷贝的子目录内文件（相对 profile 根）。记忆是 agent 身份的一部分。
_CLONE_SUBDIR_FILES = ["memories/MEMORY.md", "memories/USER.md"]

# --clone-all 全量拷贝后要剥掉的运行时文件（不该带过去）。
_CLONE_ALL_STRIP = ["gateway.pid", "gateway_state.json", "processes.json"]

# --clone-all 时始终排除的历史/重型产物（新 profile 应是干净工作区；继承源会话历史
# 既无用又可能让拷贝膨胀到数十 GB）。对标 Hermes _CLONE_ALL_HISTORY_EXCLUDE_ROOT。
_CLONE_ALL_EXCLUDE = frozenset({
    "state.db", "state.db-wal", "state.db-shm",
    "sessions", "backups", "state-snapshots", "checkpoints",
    "profiles", "node_modules", ".git", "__pycache__",
})

# 技能扫描跳过的目录（VCS / 依赖 / 缓存 / 市场元数据）。
_EXCLUDED_SKILL_DIRS = frozenset({
    ".git", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".pytest_cache", ".DS_Store",
})


@dataclass
class ProfileInfo:
    """一个 profile 的摘要信息（``list_profiles`` 的返回元素）。

    相比 Hermes 精简掉了 gateway_running / alias / distribution 字段（对应功能缓建）。
    """

    name: str
    path: Path
    is_default: bool
    is_active: bool = False
    model: Optional[str] = None
    provider: Optional[str] = None
    has_env: bool = False
    skill_count: int = 0
    description: str = ""
    description_auto: bool = False


# ---------------------------------------------------------------------------
# 名字规范化与校验
# ---------------------------------------------------------------------------

def normalize_profile_name(name: str) -> str:
    """返回落盘/CLI 用的规范 profile id。

    命名 profile 一律小写存储；特殊别名 ``default`` 大小写不敏感（``Default``→``default``）。
    入口先规范化再校验，以容忍 UI/CLI 传入的标题式大小写。
    """
    if not isinstance(name, str):
        name = str(name)
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile 名不能为空")
    if stripped.casefold() == "default":
        return "default"
    return stripped.lower()


def validate_profile_name(name: str) -> None:
    """名字非法则抛 ``ValueError``（严格小写匹配，调用方应先 normalize）。

    ``default`` 是内置根 profile 的特殊别名，直接放行。
    """
    if name == "default":
        return
    if not _PROFILE_ID_RE.match(name):
        raise ValueError(
            f"非法 profile 名 {name!r}：须匹配 [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if name in _RESERVED_NAMES:
        raise ValueError(
            f"profile 名 {name!r} 为保留名（与 Spirit 安装或常见系统命令冲突），请换一个"
        )


def get_profile_dir(name: str) -> Path:
    """把 profile 名规范化后解析为其 HOME 目录。"""
    return _paths.profile_dir(normalize_profile_name(name))


def profile_exists(name: str) -> bool:
    """profile 目录是否存在（``default`` 恒为 True）。"""
    canon = normalize_profile_name(name)
    if canon == "default":
        return True
    return get_profile_dir(canon).is_dir()


# ---------------------------------------------------------------------------
# 配置 / 技能探测（容错，绝不因单个 profile 损坏而抛）
# ---------------------------------------------------------------------------

def _read_config_model(profile_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    """从 profile 的 config.yaml 读 (model, provider)。Spirit 配置在 ``llm`` 节下。"""
    config_path = profile_dir / "config.yaml"
    if not config_path.is_file():
        return None, None
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        llm = cfg.get("llm", {})
        if isinstance(llm, str):
            return llm, None
        if isinstance(llm, dict):
            return llm.get("model") or None, llm.get("provider") or None
        return None, None
    except Exception:
        return None, None


def _count_skills(profile_dir: Path) -> int:
    """统计 profile 已安装技能数（``skills/`` 下的 SKILL.md，跳过排除目录）。"""
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return 0
    count = 0
    for md in skills_dir.rglob("SKILL.md"):
        try:
            rel_parts = md.relative_to(skills_dir).parts
        except ValueError:
            continue
        if any(part in _EXCLUDED_SKILL_DIRS for part in rel_parts):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# profile.yaml — 每 profile 元数据（description 等）
# ---------------------------------------------------------------------------

def _profile_yaml_path(profile_dir: Path) -> Path:
    return profile_dir / "profile.yaml"


def read_profile_meta(profile_dir: Path) -> dict:
    """读 ``<profile_dir>/profile.yaml``，返回 ``{description, description_auto}``。

    文件缺失/损坏 → 空默认值，绝不抛（一个坏 profile.yaml 不该拖垮 list）。
    """
    path = _profile_yaml_path(profile_dir)
    default = {"description": "", "description_auto": False}
    if not path.is_file():
        return default
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return default
    if not isinstance(data, dict):
        return default
    return {
        "description": str(data.get("description") or "").strip(),
        "description_auto": bool(data.get("description_auto", False)),
    }


def write_profile_meta(
    profile_dir: Path,
    *,
    description: Optional[str] = None,
    description_auto: Optional[bool] = None,
) -> None:
    """就地更新 ``profile.yaml``：只覆盖显式传入的字段，其余保留；文件缺失则创建。"""
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile 目录不存在：{profile_dir}")
    import yaml
    path = _profile_yaml_path(profile_dir)
    existing: dict = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    if description is not None:
        existing["description"] = description.strip()
    if description_auto is not None:
        existing["description_auto"] = bool(description_auto)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# 活跃 profile（粘性状态）
# ---------------------------------------------------------------------------

def get_active_profile() -> str:
    """读粘性活跃 profile 名；文件缺失/为空 → ``"default"``。"""
    path = _paths.active_profile_path()
    try:
        name = path.read_text(encoding="utf-8").strip()
        return name or "default"
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return "default"


def set_active_profile(name: str) -> None:
    """设粘性活跃 profile（写 ``<root>/active_profile``）。传 ``"default"`` 则清除。"""
    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    if canon != "default" and not profile_exists(canon):
        raise FileNotFoundError(
            f"profile '{canon}' 不存在。先用 create_profile('{canon}') 创建"
        )
    path = _paths.active_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if canon == "default":
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(canon + "\n", encoding="utf-8")
    tmp.replace(path)  # 原子写


def get_active_profile_name() -> str:
    """从当前 SPIRIT_HOME 反推 profile 名。

    等于根目录 → ``"default"``；形如 ``<root>/profiles/<name>`` → ``<name>``；
    其余无法识别的路径 → ``"custom"``。
    """
    home = _paths._spirit_home().resolve()
    root = _paths.default_root().resolve()
    if home == root:
        return "default"
    profiles_root = _paths.profiles_root().resolve()
    try:
        rel = home.relative_to(profiles_root)
        if len(rel.parts) == 1 and _PROFILE_ID_RE.match(rel.parts[0]):
            return rel.parts[0]
    except ValueError:
        pass
    return "custom"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_profiles() -> List[ProfileInfo]:
    """返回所有 profile 的信息（含内置 default）。对单个 profile 的读取失败保持容错。"""
    active = get_active_profile()
    out: List[ProfileInfo] = []

    root = _paths.default_root()
    if root.is_dir():
        out.append(_build_info("default", root, is_default=True, active=active))

    proot = _paths.profiles_root()
    if proot.is_dir():
        for entry in sorted(proot.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == "default" or not _PROFILE_ID_RE.match(name):
                continue
            out.append(_build_info(name, entry, is_default=False, active=active))
    return out


def _build_info(name: str, path: Path, *, is_default: bool, active: str) -> ProfileInfo:
    """组装单个 ProfileInfo，所有探测都容错。"""
    try:
        model, provider = _read_config_model(path)
    except Exception:
        model, provider = None, None
    try:
        meta = read_profile_meta(path)
    except Exception:
        meta = {"description": "", "description_auto": False}
    try:
        skill_count = _count_skills(path)
    except Exception:
        skill_count = 0
    return ProfileInfo(
        name=name,
        path=path,
        is_default=is_default,
        is_active=(name == active),
        model=model,
        provider=provider,
        has_env=(path / ".env").exists(),
        skill_count=skill_count,
        description=meta.get("description", ""),
        description_auto=meta.get("description_auto", False),
    )


def _clone_all_ignore(source_dir: Path):
    """``shutil.copytree`` 的 ignore 回调：排除历史/重型产物与 profiles 自身。"""
    def _ignore(directory: str, names: List[str]) -> List[str]:
        # 顶层目录额外排除 _CLONE_ALL_EXCLUDE；任何层级都排除 __pycache__/.git。
        is_top = Path(directory) == source_dir
        skipped = []
        for n in names:
            if n in ("__pycache__", ".git") or (is_top and n in _CLONE_ALL_EXCLUDE):
                skipped.append(n)
        return skipped
    return _ignore


def create_profile(
    name: str,
    *,
    clone_from: Optional[str] = None,
    clone_all: bool = False,
    clone_config: bool = False,
    description: Optional[str] = None,
) -> Path:
    """创建新 profile 目录，返回其路径。

    参数
    ----
    name: profile 标识（小写字母数字 + -/_）。
    clone_from: 源 profile 名；为 None 且 clone_all/clone_config 为真时，默认从当前活跃 profile 克隆。
    clone_all: 全量 copytree 源 profile（排除历史/重型产物），克隆后立即可用。
    clone_config: 只拷配置文件（config.yaml/.env/SOUL.md）+ 技能 + 选定身份文件。
    description: 若给出，写入 profile.yaml（description_auto=False，视为用户手写）。
    """
    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    if canon == "default":
        raise ValueError("不能创建名为 'default' 的 profile——它是内置根 profile（SPIRIT_HOME 本身）")

    profile_dir = get_profile_dir(canon)
    if profile_dir.exists():
        raise FileExistsError(f"profile '{canon}' 已存在于 {profile_dir}")

    # 解析克隆源
    source_dir: Optional[Path] = None
    if clone_from is not None or clone_all or clone_config:
        if clone_from is None:
            source_dir = get_profile_dir(get_active_profile())
        else:
            src_canon = normalize_profile_name(clone_from)
            validate_profile_name(src_canon)
            source_dir = get_profile_dir(src_canon)
        if not source_dir.is_dir():
            raise FileNotFoundError(f"源 profile '{clone_from or 'active'}' 不存在于 {source_dir}")

    if clone_all and source_dir:
        shutil.copytree(source_dir, profile_dir, symlinks=True, ignore=_clone_all_ignore(source_dir))
        for stale in _CLONE_ALL_STRIP:
            (profile_dir / stale).unlink(missing_ok=True)
    else:
        profile_dir.mkdir(parents=True, exist_ok=True)
        for subdir in _PROFILE_DIRS:
            (profile_dir / subdir).mkdir(parents=True, exist_ok=True)
        if source_dir is not None and (clone_config or clone_from is not None):
            for filename in _CLONE_CONFIG_FILES:
                src = source_dir / filename
                if src.is_file():
                    dst = profile_dir / filename
                    shutil.copy2(src, dst)
                    if filename == ".env":
                        _tighten_env_perms(dst)
            source_skills = source_dir / "skills"
            if source_skills.is_dir():
                shutil.copytree(source_skills, profile_dir / "skills", symlinks=True, dirs_exist_ok=True)
            for relpath in _CLONE_SUBDIR_FILES:
                src = source_dir / relpath
                if src.is_file():
                    dst = profile_dir / relpath
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

    # 引导一个空 .env，让 profile 从第一天起就有自己的凭据文件（克隆已带则跳过）。
    env_path = profile_dir / ".env"
    if not env_path.exists():
        try:
            env_path.write_text(
                "# 本 profile 的独立密钥。\n"
                "# 此处设置的 API key / token 覆盖 shell 环境。\n"
                "# 行为配置请写 config.yaml，而非此文件。\n",
                encoding="utf-8",
            )
            _tighten_env_perms(env_path)
        except OSError:
            pass  # 尽力而为——按需写入时会再创建

    # 描述最后写，避免部分失败时留下孤立元数据。
    if description and description.strip():
        try:
            write_profile_meta(profile_dir, description=description.strip(), description_auto=False)
        except Exception:
            logger.debug("create_profile: 写 description 失败（非致命）", exc_info=True)

    return profile_dir


def _tighten_env_perms(env_path: Path) -> None:
    """把 .env 收紧为 owner-only（Windows 上 chmod 语义有限，best-effort）。"""
    try:
        os.chmod(str(env_path), 0o600)
    except OSError:
        pass


def _rmtree_with_retry(profile_dir: Path, attempts: int = 3, delay: float = 0.1) -> None:
    """带重试的 rmtree（Windows 上文件句柄短暂占用时更稳）。"""
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            shutil.rmtree(profile_dir)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(delay * (i + 1))
    if last_exc:
        raise last_exc


def delete_profile(name: str, *, yes: bool = False) -> Path:
    """删除 profile 目录，返回被删路径。不可删 default；若删的是活跃 profile 则清除粘性状态。

    ``yes`` 预留给上层做二次确认（本函数不交互）。
    """
    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    if canon == "default":
        raise ValueError("不能删除内置 default profile（它就是 SPIRIT_HOME 本身）")
    profile_dir = get_profile_dir(canon)
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile '{canon}' 不存在于 {profile_dir}")

    _rmtree_with_retry(profile_dir)

    # 若删的是活跃 profile，清除粘性状态回到 default。
    try:
        if get_active_profile() == canon:
            set_active_profile("default")
    except Exception:
        logger.debug("delete_profile: 清除 active 失败（非致命）", exc_info=True)
    return profile_dir


def rename_profile(old_name: str, new_name: str) -> Path:
    """重命名 profile（移动目录），返回新路径。若旧名是活跃 profile 则同步更新。"""
    old_canon = normalize_profile_name(old_name)
    new_canon = normalize_profile_name(new_name)
    validate_profile_name(old_canon)
    validate_profile_name(new_canon)
    if old_canon == "default" or new_canon == "default":
        raise ValueError("default profile 不可重命名，也不可用作重命名目标")
    old_dir = get_profile_dir(old_canon)
    new_dir = get_profile_dir(new_canon)
    if not old_dir.is_dir():
        raise FileNotFoundError(f"profile '{old_canon}' 不存在于 {old_dir}")
    if new_dir.exists():
        raise FileExistsError(f"profile '{new_canon}' 已存在于 {new_dir}")

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))

    try:
        if get_active_profile() == old_canon:
            set_active_profile(new_canon)
    except Exception:
        logger.debug("rename_profile: 同步 active 失败（非致命）", exc_info=True)
    return new_dir
