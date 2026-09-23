"""技能市场（Hub）— Spirit Agent（Phase 4.6）。

对标 Hermes ``tools/skills_hub.py`` 的核心：从外部源获取技能捆绑 → 隔离 → 安全扫描 →
安装（或留在隔离区）→ 记锁文件 / 审计日志 / 来源。补齐 ``spirit.tools.skills`` 里早已
声明但未实现的 ``HUB_DIR`` / ``LOCK_FILE`` / ``QUARANTINE_DIR`` / ``AUDIT_LOG`` 常量对应的
市场逻辑。

**可离线测试**是本模块的第一设计约束（对齐用户「和 Hermes 一样的任务检查测试用例」）：
获取（网络）被隔离到一个可注入的 seam —— :func:`fetch_bundle` / :func:`set_fetcher` /
:func:`register_source`；核心安装流 :func:`install_bundle` 只吃一个内存里的
:class:`SkillBundle`，纯磁盘操作，测试可直接构造 bundle 断言 lock / quarantine / audit /
落盘文件，无需触网。

安全边界（逐条对标 Hermes，全是 rmtree-escape / 路径穿越 / 符号链接重定向的防线）：

- 技能名 / 相对路径 / 安装父路径经 ``_normalize_bundle_path`` 校验：拒空、拒绝对、拒
  ``..``、拒 Windows 盘符、按需拒嵌套。
- 锁文件的 ``install_path`` 经 ``_normalize_lock_install_path`` 强制以 ``<skill_name>`` 结尾
  （否则 uninstall 的 rmtree 可能抹掉整个 skills 树）。
- ``_resolve_lock_install_path`` 逐段拼路径并拒绝符号链接 / junction 重定向。
- 安装前扫描：``dangerous`` 一律拒绝（留隔离区）；``caution`` 仅受信源或显式 ``allow_caution``
  才装；``safe`` 直接装。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from spirit.skills_hub import paths, provenance

logger = logging.getLogger(__name__)


class HubError(Exception):
    """市场操作的可预期失败（源不可用 / 非法标识符 / 被扫描拒绝）。"""


class HubSourceUnavailable(HubError):
    """未配置可用的获取源（无网络 fetcher 注册）。"""


# ---------------------------------------------------------------------------
# 数据模型（对标 Hermes SkillBundle）
# ---------------------------------------------------------------------------

@dataclass
class SkillBundle:
    """一个已下载、待隔离 / 扫描 / 安装的技能。

    ``files`` 是 ``{相对路径: 内容}``（内容为 str 或 bytes）。
    """

    name: str
    files: Dict[str, Union[str, bytes]] = field(default_factory=dict)
    source: str = "community"
    identifier: str = ""
    trust_level: str = "community"  # builtin | trusted | community
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 路径安全（rmtree-escape / 穿越 / 重定向防线）
# ---------------------------------------------------------------------------

def _is_path_redirect(path: Path) -> bool:
    """``path`` 是符号链接或（Windows 上）目录 junction 时为 True。"""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(Path, "is_junction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except (OSError, AttributeError):
            return False
    return False


def _normalize_bundle_path(path_value: str, *, field_name: str, allow_nested: bool) -> str:
    """触盘前归一化并校验 bundle 控制的路径。"""
    if not isinstance(path_value, str):
        raise ValueError(f"不安全的 {field_name}：期望字符串")
    raw = path_value.strip()
    if not raw:
        raise ValueError(f"不安全的 {field_name}：空路径")

    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = [p for p in path.parts if p not in {"", "."}]

    if normalized.startswith("/") or path.is_absolute():
        raise ValueError(f"不安全的 {field_name}：{path_value}")
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"不安全的 {field_name}：{path_value}")
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        raise ValueError(f"不安全的 {field_name}：{path_value}")
    if not allow_nested and len(parts) != 1:
        raise ValueError(f"不安全的 {field_name}：{path_value}")
    return "/".join(parts)


def _validate_skill_name(name: str) -> str:
    return _normalize_bundle_path(name, field_name="技能名", allow_nested=False)


def _validate_install_parent_path(category: str) -> str:
    return _normalize_bundle_path(category, field_name="安装父路径", allow_nested=True)


def _validate_bundle_rel_path(rel_path: str) -> str:
    return _normalize_bundle_path(rel_path, field_name="文件路径", allow_nested=True)


def _normalize_lock_install_path(install_path: str, skill_name: str) -> str:
    """校验锁文件 ``install_path``：必须以 ``<skill_name>`` 结尾（防 uninstall rmtree 逃逸）。"""
    safe_skill_name = _validate_skill_name(skill_name)
    normalized = _normalize_bundle_path(install_path, field_name="安装路径", allow_nested=True)
    parts = normalized.split("/")
    if not parts or parts[-1] != safe_skill_name:
        raise ValueError(f"不安全的安装路径：{install_path}")
    return normalized


def _resolve_lock_install_path(install_path: str, skill_name: str) -> Path:
    """把锁文件 ``install_path`` 解析为 skills 目录下的绝对路径，逐段拒绝重定向。"""
    normalized = _normalize_lock_install_path(install_path, skill_name)
    base = paths.skills_dir()
    current = base
    for part in normalized.split("/"):
        current = current / part
        if _is_path_redirect(current):
            raise ValueError(f"拒绝跟随符号链接 / junction：{current}")
    return current


# ---------------------------------------------------------------------------
# 内容哈希
# ---------------------------------------------------------------------------

def bundle_content_hash(bundle: SkillBundle) -> str:
    """内存 bundle 的确定性哈希（含路径，故文件内容互换会改变哈希）。"""
    h = hashlib.sha256()
    for rel_path in sorted(bundle.files):
        h.update(rel_path.encode("utf-8"))
        h.update(b"\x00")
        content = bundle.files[rel_path]
        h.update(content if isinstance(content, bytes) else content.encode("utf-8"))
    return f"sha256:{h.hexdigest()[:16]}"


def content_hash(directory: Path) -> str:
    """磁盘上技能目录的确定性哈希（遍历排序后的常规文件）。"""
    h = hashlib.sha256()
    try:
        entries = sorted(p for p in directory.rglob("*") if p.is_file())
    except OSError:
        return "sha256:"
    for f in entries:
        try:
            rel = str(f.relative_to(directory)).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(f.read_bytes())
        except OSError:
            continue
    return f"sha256:{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# 锁文件
# ---------------------------------------------------------------------------

class HubLockFile:
    """管理 ``skills/.hub/lock.json`` —— 追踪已安装市场技能的来源。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else paths.lock_file()

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "installed": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("installed"), dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "installed": {}}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def record_install(
        self,
        name: str,
        source: str,
        identifier: str,
        trust_level: str,
        scan_verdict: str,
        skill_hash: str,
        install_path: str,
        files: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """写入 / 更新一条安装记录（写时校验名与路径形状，杜绝坏状态入锁）。"""
        safe_name = _validate_skill_name(name)
        safe_install_path = _normalize_lock_install_path(install_path, safe_name)
        data = self.load()
        now = datetime.now(timezone.utc).isoformat()
        existing = data["installed"].get(safe_name, {})
        data["installed"][safe_name] = {
            "source": source,
            "identifier": identifier,
            "trust_level": trust_level,
            "scan_verdict": scan_verdict,
            "content_hash": skill_hash,
            "install_path": safe_install_path,
            "files": files,
            "metadata": metadata or {},
            "installed_at": existing.get("installed_at") or now,
            "updated_at": now,
        }
        self.save(data)

    def record_uninstall(self, name: str) -> None:
        data = self.load()
        data["installed"].pop(name, None)
        self.save(data)

    def get_installed(self, name: str) -> Optional[dict]:
        return self.load()["installed"].get(name)

    def list_installed(self) -> List[dict]:
        return [{"name": n, **entry} for n, entry in self.load()["installed"].items()]


# ---------------------------------------------------------------------------
# Taps（自定义源）
# ---------------------------------------------------------------------------

class TapsManager:
    """管理 ``taps.json`` —— 自定义 GitHub 仓库源。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else paths.taps_file()

    def load(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            taps = data.get("taps", [])
            return taps if isinstance(taps, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, taps: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"taps": taps}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def add(self, repo: str, path: str = "skills/") -> bool:
        taps = self.load()
        if any(t.get("repo") == repo for t in taps):
            return False
        taps.append({"repo": repo, "path": path})
        self.save(taps)
        return True

    def remove(self, repo: str) -> bool:
        taps = self.load()
        new_taps = [t for t in taps if t.get("repo") != repo]
        if len(new_taps) == len(taps):
            return False
        self.save(new_taps)
        return True

    def list_taps(self) -> List[dict]:
        return self.load()


# ---------------------------------------------------------------------------
# 审计日志（JSONL）
# ---------------------------------------------------------------------------

def append_audit_log(
    action: str,
    skill_name: str,
    source: str = "",
    trust_level: str = "",
    verdict: str = "",
    extra: str = "",
) -> None:
    """向 ``audit.jsonl`` 追加一行 JSON 审计记录。"""
    audit = paths.audit_log()
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "skill": skill_name,
        "source": source,
        "trust_level": trust_level,
        "verdict": verdict,
    }
    if extra:
        record["extra"] = extra
    try:
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug("无法写审计日志: %s", exc)


def read_audit_log() -> List[dict]:
    """读取审计日志全部记录（供 ``/skill audit`` 展示）。"""
    audit = paths.audit_log()
    if not audit.exists():
        return []
    out: List[dict] = []
    try:
        for line in audit.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def ensure_hub_dirs() -> None:
    """创建 ``.hub`` 目录结构（幂等）。"""
    paths.hub_dir().mkdir(parents=True, exist_ok=True)
    paths.quarantine_dir().mkdir(parents=True, exist_ok=True)
    paths.index_cache_dir().mkdir(parents=True, exist_ok=True)
    lock = paths.lock_file()
    if not lock.exists():
        lock.write_text('{"version": 1, "installed": {}}\n', encoding="utf-8")
    if not paths.audit_log().exists():
        paths.audit_log().touch()
    taps = paths.taps_file()
    if not taps.exists():
        taps.write_text('{"taps": []}\n', encoding="utf-8")


# ---------------------------------------------------------------------------
# 索引缓存
# ---------------------------------------------------------------------------

def _index_cache_file(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
    return paths.index_cache_dir() / f"{safe}.json"


def read_index_cache(key: str) -> Optional[Any]:
    """读索引缓存（不存在 / 损坏 / 过期返回 None）。"""
    path = _index_cache_file(key)
    if not path.exists():
        return None
    try:
        from spirit.config import get_config_value
        ttl = int(get_config_value("internal.skills_index_cache_ttl", 3600))
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age > ttl:
            return None
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        return wrapper.get("data") if isinstance(wrapper, dict) else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def write_index_cache(key: str, data: Any) -> None:
    """写索引缓存（包一层 ``{"data": ...}`` 以便按 mtime 判过期）。"""
    path = _index_cache_file(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"data": data}, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError) as exc:
        logger.debug("无法写索引缓存: %s", exc)


# ---------------------------------------------------------------------------
# 获取 seam（网络隔离点，测试可注入）
# ---------------------------------------------------------------------------

_FETCHERS: Dict[str, Callable[[str], SkillBundle]] = {}
_DEFAULT_FETCHER: Optional[Callable[[str, str], SkillBundle]] = None


def register_source(source_name: str, fetcher: Callable[[str], SkillBundle]) -> None:
    """注册一个具名源的获取器（``fetcher(identifier) -> SkillBundle``）。"""
    _FETCHERS[source_name] = fetcher


def set_fetcher(fetcher: Optional[Callable[[str, str], SkillBundle]]) -> None:
    """设置全局默认获取器（``fetcher(identifier, source) -> SkillBundle``）；None 清除。

    测试注入点：monkeypatch 此函数即可完全离线驱动 :func:`fetch_bundle`。
    """
    global _DEFAULT_FETCHER
    _DEFAULT_FETCHER = fetcher


def fetch_bundle(identifier: str, source: str = "community") -> SkillBundle:
    """获取一个技能 bundle（先全局默认 fetcher，再具名源；都无则抛 HubSourceUnavailable）。"""
    ident = (identifier or "").strip()
    if not ident:
        raise HubError("技能标识符不能为空")
    if _DEFAULT_FETCHER is not None:
        return _DEFAULT_FETCHER(ident, source)
    fetcher = _FETCHERS.get(source)
    if fetcher is not None:
        return fetcher(ident)
    raise HubSourceUnavailable(
        f"未配置源 '{source}' 的获取器（离线环境请用 set_fetcher / register_source 注入）"
    )


# ---------------------------------------------------------------------------
# 隔离 / 安装 / 卸载
# ---------------------------------------------------------------------------

def quarantine_bundle(bundle: SkillBundle) -> Path:
    """把 bundle 写入隔离区待扫描，返回隔离目录（校验每个相对路径防穿越）。"""
    ensure_hub_dirs()
    skill_name = _validate_skill_name(bundle.name)
    validated: List[Tuple[str, Union[str, bytes]]] = []
    for rel_path, file_content in bundle.files.items():
        safe_rel = _validate_bundle_rel_path(rel_path)
        validated.append((safe_rel, file_content))

    dest = paths.quarantine_dir() / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for rel_path, file_content in validated:
        file_dest = dest.joinpath(*rel_path.split("/"))
        file_dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(file_content, bytes):
            file_dest.write_bytes(file_content)
        else:
            file_dest.write_text(file_content, encoding="utf-8")
    return dest


def install_from_quarantine(
    quarantine_path: Path,
    skill_name: str,
    category: str,
    bundle: SkillBundle,
    scan_verdict: str,
) -> Path:
    """把已扫描的技能从隔离区移入 skills 目录，记锁文件 + 审计 + 来源。"""
    safe_skill_name = _validate_skill_name(skill_name)
    safe_category = _validate_install_parent_path(category) if category else ""

    quarantine_resolved = quarantine_path.resolve()
    quarantine_root = paths.quarantine_dir().resolve()
    if not quarantine_resolved.is_relative_to(quarantine_root):
        raise ValueError(f"不安全的隔离路径：{quarantine_path}")

    install_rel_path = f"{safe_category}/{safe_skill_name}" if safe_category else safe_skill_name
    install_dir = _resolve_lock_install_path(install_rel_path, safe_skill_name)

    if install_dir.exists():
        shutil.rmtree(install_dir)

    # 拒绝隔离技能内的符号链接（否则其目标内容会被复制进 skills 树并泄漏给 agent）。
    for entry in quarantine_path.rglob("*"):
        if not _is_path_redirect(entry):
            continue
        try:
            rel = entry.relative_to(quarantine_resolved)
        except ValueError:
            rel = entry
        raise ValueError(f"安装的技能含符号链接，不允许：{rel}")

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(quarantine_path), str(install_dir))

    skill_hash = content_hash(install_dir)
    lock = HubLockFile()
    lock.record_install(
        name=safe_skill_name,
        source=bundle.source,
        identifier=bundle.identifier,
        trust_level=bundle.trust_level,
        scan_verdict=scan_verdict,
        skill_hash=skill_hash,
        install_path=str(install_dir.relative_to(paths.skills_dir())).replace("\\", "/"),
        files=list(bundle.files.keys()),
        metadata=bundle.metadata,
    )
    append_audit_log("INSTALL", safe_skill_name, bundle.source, bundle.trust_level, scan_verdict, skill_hash)
    provenance.record_install(
        safe_skill_name,
        source=bundle.source,
        identifier=bundle.identifier,
        trust_level=bundle.trust_level,
        source_url=str(bundle.metadata.get("source_url") or bundle.identifier),
        content_hash=skill_hash,
    )
    return install_dir


def uninstall_skill(skill_name: str) -> Tuple[bool, str]:
    """移除一个市场安装的技能（拒绝移除非市场安装的 / 内置技能）。"""
    lock = HubLockFile()
    entry = lock.get_installed(skill_name)
    if not entry:
        return False, f"'{skill_name}' 不是市场安装的技能（可能是内置或本地技能）"

    # 破坏性边界：落到 rmtree 的路径必须在 SKILLS_DIR 内且不得是 SKILLS_DIR 本身。
    try:
        install_path = _resolve_lock_install_path(entry.get("install_path", ""), skill_name)
    except ValueError as exc:
        return False, f"拒绝卸载 '{skill_name}'：{exc}"

    if install_path.exists():
        shutil.rmtree(install_path)

    lock.record_uninstall(skill_name)
    provenance.clear_install(skill_name)
    append_audit_log("UNINSTALL", skill_name, entry.get("source", ""), entry.get("trust_level", ""), "n/a", "user_request")
    return True, f"已从 {entry.get('install_path', '')} 卸载 '{skill_name}'"


def _scan_quarantined(quarantine_path: Path, source: str):
    """对隔离目录跑安全扫描（延迟导入 guard 避免包级循环）。"""
    from spirit.tools.skills import scan_skill

    return scan_skill(quarantine_path, source=source)


def install_bundle(
    bundle: SkillBundle,
    *,
    category: str = "",
    scan: bool = True,
    allow_caution: bool = False,
) -> Tuple[bool, str]:
    """安装一个内存 bundle 的核心流：隔离 → 扫描 → 安装或留隔离。返回 ``(ok, message)``。

    纯磁盘操作、不触网，故可完全离线单测。裁决策略：

    - ``dangerous``（含 critical 发现）→ **拒绝**，留在隔离区，审计 QUARANTINE。
    - ``caution``（含 high 发现）→ 仅 ``trust_level == "trusted"`` 或 ``allow_caution`` 才装，
      否则拒绝并留隔离区。
    - ``safe`` → 直接装。
    - ``scan=False`` → 跳过扫描（仅受信内部调用），verdict 记为 "unscanned"。
    """
    try:
        quarantine_path = quarantine_bundle(bundle)
    except ValueError as exc:
        append_audit_log("REJECT", bundle.name, bundle.source, bundle.trust_level, "unsafe_path", str(exc))
        return False, f"拒绝安装 '{bundle.name}'：{exc}"

    verdict = "unscanned"
    if scan:
        result = _scan_quarantined(quarantine_path, source=bundle.source)
        verdict = getattr(result, "verdict", "safe")

    trusted = bundle.trust_level in ("builtin", "trusted")

    if verdict == "dangerous" or (verdict == "caution" and not trusted and not allow_caution):
        append_audit_log("QUARANTINE", bundle.name, bundle.source, bundle.trust_level, verdict,
                         str(quarantine_path))
        reason = "危险" if verdict == "dangerous" else "存疑（caution）且非受信源"
        return False, (
            f"已把 '{bundle.name}' 隔离（扫描裁决：{verdict}，{reason}）。"
            f"隔离位置：{quarantine_path}"
        )

    try:
        install_dir = install_from_quarantine(quarantine_path, bundle.name, category, bundle, verdict)
    except ValueError as exc:
        append_audit_log("REJECT", bundle.name, bundle.source, bundle.trust_level, verdict, str(exc))
        return False, f"拒绝安装 '{bundle.name}'：{exc}"
    return True, f"已安装 '{bundle.name}' 到 {install_dir}（扫描裁决：{verdict}）"


def install_skill(
    identifier: str,
    *,
    source: str = "community",
    category: str = "",
    allow_caution: bool = False,
) -> Tuple[bool, str]:
    """获取并安装一个技能（``fetch_bundle`` seam → :func:`install_bundle`）。"""
    try:
        bundle = fetch_bundle(identifier, source=source)
    except HubError as exc:
        return False, str(exc)
    return install_bundle(bundle, category=category, allow_caution=allow_caution)


# ---------------------------------------------------------------------------
# 浏览 / 搜索（索引缓存驱动，离线友好）
# ---------------------------------------------------------------------------

def browse_index(source: str = "community", *, refresh: bool = False) -> List[dict]:
    """浏览某源的可用技能索引（缓存优先；``refresh`` 或缓存失效时经 seam 重建）。

    索引由注册的源索引器产出；无网络源时返回缓存（可能为空）。测试可经
    :func:`write_index_cache` 预置索引，或 monkeypatch ``_index_fetcher``。
    """
    cache_key = f"index-{source}"
    if not refresh:
        cached = read_index_cache(cache_key)
        if isinstance(cached, list):
            return cached
    fetcher = _INDEX_FETCHERS.get(source)
    if fetcher is None:
        cached = read_index_cache(cache_key)
        return cached if isinstance(cached, list) else []
    try:
        entries = fetcher()
    except Exception as exc:
        logger.debug("索引获取失败（%s）: %s", source, exc)
        cached = read_index_cache(cache_key)
        return cached if isinstance(cached, list) else []
    if isinstance(entries, list):
        write_index_cache(cache_key, entries)
        return entries
    return []


_INDEX_FETCHERS: Dict[str, Callable[[], List[dict]]] = {}


def register_index_source(source_name: str, index_fetcher: Callable[[], List[dict]]) -> None:
    """注册某源的索引获取器（``index_fetcher() -> [entry, ...]``）。"""
    _INDEX_FETCHERS[source_name] = index_fetcher


def search_skills(query: str = "", *, source: str = "community") -> List[dict]:
    """在索引里按名 / 描述 / 标签子串搜索（大小写不敏感；空 query 返回全部）。"""
    entries = browse_index(source)
    q = (query or "").strip().lower()
    if not q:
        return entries
    out: List[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        haystack = " ".join([
            str(entry.get("name", "")),
            str(entry.get("description", "")),
            " ".join(str(t) for t in (entry.get("tags") or [])),
        ]).lower()
        if q in haystack:
            out.append(entry)
    return out


__all__ = [
    "HubError",
    "HubSourceUnavailable",
    "SkillBundle",
    "HubLockFile",
    "TapsManager",
    "append_audit_log",
    "read_audit_log",
    "ensure_hub_dirs",
    "read_index_cache",
    "write_index_cache",
    "register_source",
    "set_fetcher",
    "fetch_bundle",
    "quarantine_bundle",
    "install_from_quarantine",
    "uninstall_skill",
    "install_bundle",
    "install_skill",
    "browse_index",
    "register_index_source",
    "search_skills",
    "bundle_content_hash",
    "content_hash",
]
