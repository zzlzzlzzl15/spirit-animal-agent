"""权威的 项目 → 仓库 → 泳道 → 会话 树构建器 — Spirit Agent（Phase 4.4）。

对标 Hermes ``tui_gateway/project_tree.py``（纯 stdlib 移植，逻辑逐字对齐）：这是桌面侧边栏
把会话分组进 项目 / 仓库 / 泳道 的**唯一真相源**。它是纯函数（所有 git 解析经 ``resolve``
注入），故可用 fixture 离线单测，并被网关的 ``projects.tree`` / ``projects.project_sessions``
RPC 复用。

id 与泳道键约定（与渲染器持久化状态——置顶 / 手动排序 / 忽略——字节兼容，全部以这些字符串为键）：

  - 显式项目 id .......... ``p_<hex>``（来自 projects.db）
  - 自动/发现项目 id ..... 仓库根路径
  - 仓库节点 id .......... 仓库根路径
  - 主分支泳道 id ........ ``<repoRoot>::branch::<branch>``（或 ``::branch::``）
  - kanban 桶泳道 id ..... ``<repoRoot>::kanban``
  - 链接 worktree 泳道 id  worktree 路径

相较早期客户端版本的一处正确性升级：链接 worktree 经 git common-dir 探测（注入为 ``resolve``）
折叠进其**主**仓库，而非当作独立仓库（``git rev-parse --show-toplevel`` 返回 worktree 自己的根，
这正是客户端重复计数的原因）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

# cwd → git 身份解析器。返回 ``{"repo_root", "worktree_root"}``，其中 ``repo_root`` 是跨 worktree
# 共享的**公共**（主）仓库根，``worktree_root`` 是本 cwd 自己的检出根。cwd 不在 git 仓库内
# （或无法探测，如远程后端）时返回 ``None``。
Resolve = Callable[[str], Optional[dict]]

# 只有 KANBAN-TASK worktree（``<repo>/.worktrees/t_<hex>``，kanban_db 铸造的 ``t_…`` id）折叠进
# 同一泳道；``.worktrees/`` 下用户命名的 "New worktree" 目录保留为各自的泳道。
_KANBAN_DIR_RE = re.compile(r"^(.*[/\\]\.worktrees)[/\\]t_[0-9a-f]+[/\\]?$")
_TRUNK_BRANCHES = {"main", "master", "trunk", "develop"}
DEFAULT_BRANCH_LABEL = "main"


def _branch_lane_id(repo_root: str, branch: str = "") -> str:
    """主检出泳道 id 的唯一定义（必须与桌面端一致）。"""
    return f"{repo_root}::branch::{(branch or '').strip()}"


def _kanban_lane_id(repo_root: str) -> str:
    return f"{repo_root}::kanban"


# ---------------------------------------------------------------------------
# 路径 helpers（匹配 TS 分段逻辑，使 label/id 对齐）
# ---------------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    return [s for s in re.split(r"[/\\]", (path or "").rstrip("/\\")) if s]


def _is_windows_path(path: str) -> bool:
    value = (path or "").strip()
    # 盘符（``C:\…``）、UNC（``\\srv``、``//srv``）、或任何反斜杠根路径——含根相对的
    # ``\wsl.localhost\…`` / ``\Users\…`` 拼写。单个前导 ``/`` 保持 POSIX（大小写敏感）。
    return bool(re.match(r"^[A-Za-z]:[/\\]", value)) or value.startswith(("\\", "//"))


def _comparison_segments(path: str) -> list[str]:
    """适合在任意主机上做身份比较的路径分段。

    Windows 路径即便测试或远程后端跑在 POSIX 上仍保持大小写不敏感。显示路径与发出的 id
    保留其原始拼写。
    """
    segs = _segments(path)
    return [segment.casefold() for segment in segs] if _is_windows_path(path) else segs


def _path_key(path: str) -> str:
    """规范比较键（分隔符 / 尾斜杠无关）。"""
    return "/".join(_comparison_segments(path))


def _lane_key(path_or_lane: str) -> str:
    """只规范化泳道 id 的路径部分。

    分支标签保持字节原样；仓库 / worktree 路径遵循平台路径身份，故等价的 Windows 拼写不会
    创建重复泳道。
    """
    for marker in ("::branch::", "::kanban"):
        if marker in path_or_lane:
            root, suffix = path_or_lane.split(marker, 1)
            return f"{_path_key(root)}{marker}{suffix}"
    return _path_key(path_or_lane)


def base_name(path: str) -> str:
    segs = _segments(path)
    return segs[-1] if segs else ""


def kanban_worktree_dir(path: str) -> Optional[str]:
    """``.../.worktrees/<task>`` 路径的 ``<repo>/.worktrees`` 目录，否则 None。"""
    m = _KANBAN_DIR_RE.match(path or "")
    return m.group(1) if m else None


def _is_path_under(folder: str, target: str) -> bool:
    """``target`` 等于 ``folder`` 或（按分段）嵌套其下时为 True。"""
    f = _comparison_segments(folder)
    t = _comparison_segments(target)
    if not f or len(f) > len(t):
        return False
    return all(f[i] == t[i] for i in range(len(f)))


def _with_base_name(path: str, name: str) -> str:
    stripped = re.sub(r"[/\\]+$", "", path)
    return re.sub(r"[^/\\]+$", name, stripped)


# ---------------------------------------------------------------------------
# 泳道放置
# ---------------------------------------------------------------------------


def _placement(
    repo_root: str,
    lane_key: str,
    lane_label: str,
    lane_path: str,
    is_main: bool,
    is_kanban: bool,
) -> dict:
    return {
        "repo_key": repo_root,
        "repo_label": base_name(repo_root) or repo_root,
        "repo_path": repo_root,
        "lane_key": lane_key,
        "lane_label": lane_label,
        "lane_path": lane_path,
        "is_main": is_main,
        "is_kanban": is_kanban,
    }


def _place_by_heuristic(path: str) -> Optional[dict]:
    """无 git 探测且无持久化根时的纯路径回退。"""
    base = base_name(path)
    if not base:
        return None

    kanban_dir = kanban_worktree_dir(path)
    if kanban_dir:
        repo_path = re.sub(r"[/\\]+$", "", _with_base_name(kanban_dir, ""))
        return _placement(repo_path, _kanban_lane_id(repo_path), "kanban", kanban_dir, False, True)

    m = re.match(r"^(.+)-wt-(.+)$", base)
    if m:
        repo_path = _with_base_name(path, m.group(1))
        return _placement(repo_path, path, m.group(2), path, False, False)

    return _placement(path, path, base, path, True, False)


def _place(cwd: str, branch: str, resolve: Optional[Resolve], persisted_root: str) -> Optional[dict]:
    info = resolve(cwd) if resolve else None

    if info and info.get("repo_root") and info.get("worktree_root"):
        repo_root = info["repo_root"]
        worktree_root = info["worktree_root"]
        is_main = worktree_root == repo_root or bool(info.get("is_main"))

        if is_main:
            # 未记录分支折叠进唯一 trunk 泳道，故一个仓库绝不显示两个 "main" 泳道
            # （记录的 "main" + 空分支桶）。
            b = (branch or "").strip() or DEFAULT_BRANCH_LABEL
            return _placement(repo_root, _branch_lane_id(repo_root, b), b, repo_root, True, False)

        kanban_dir = kanban_worktree_dir(worktree_root)
        if kanban_dir:
            return _placement(repo_root, _kanban_lane_id(repo_root), "kanban", kanban_dir, False, True)

        label = base_name(worktree_root) or worktree_root
        return _placement(repo_root, worktree_root, label, worktree_root, False, False)

    # 无实时探测：信任后端持久化的根（按其分组，用会话记录的分支切分主检出）。
    # kanban 任务仍按路径形状折叠。
    if persisted_root:
        kanban_dir = kanban_worktree_dir(cwd)
        if kanban_dir:
            return _placement(persisted_root, _kanban_lane_id(persisted_root), "kanban", kanban_dir, False, True)
        b = (branch or "").strip() or DEFAULT_BRANCH_LABEL
        return _placement(persisted_root, _branch_lane_id(persisted_root, b), b, persisted_root, True, False)

    return _place_by_heuristic(cwd)


def _session_repo_root(session: dict, resolve: Optional[Resolve]) -> str:
    """会话所属的**公共**仓库根（折叠链接 worktree）。"""
    cwd = (session.get("cwd") or "").strip()
    if cwd and resolve:
        info = resolve(cwd)
        if info and info.get("repo_root"):
            return info["repo_root"]
    return (session.get("git_repo_root") or "").strip()


# ---------------------------------------------------------------------------
# 排序 + 标签消歧（与旧客户端树平价）
# ---------------------------------------------------------------------------


def _lane_sort_key(group: dict) -> tuple:
    # trunk 置顶；kanban 聚合沉底；其余（分支 + 链接 worktree）按最近活动、再按标签排序。
    is_trunk = bool(group.get("isMain")) and group["label"].lower() in _TRUNK_BRANCHES
    is_kanban = bool(group.get("isKanban"))
    activity = max((_session_time(s) for s in group.get("sessions") or []), default=0.0)
    return (
        0 if is_trunk else 1,
        1 if is_kanban else 0,
        -activity,
        group["label"].lower(),
    )


def _sort_lanes(groups: list[dict]) -> list[dict]:
    return sorted(groups, key=_lane_sort_key)


def _disambiguate_labels(items: list[dict]) -> None:
    """把冲突的 basename 长成带路径前缀的标签（就地）。"""
    by_label: dict[str, list[dict]] = {}
    for item in items:
        by_label.setdefault(item["label"], []).append(item)

    for bucket in by_label.values():
        pathed = [g for g in bucket if g.get("path")]
        if len(pathed) < 2:
            continue

        parents = {id(g): _segments(g["path"])[:-1] for g in pathed}
        max_depth = max(len(p) for p in parents.values())
        depth = 1
        while depth <= max_depth:
            counts: dict[str, int] = {}
            for g in pathed:
                segs = parents[id(g)]
                prefix = "/".join(segs[-depth:]) if depth else ""
                base = base_name(g["path"]) or g["path"]
                g["label"] = f"{prefix}/{base}" if prefix else base
                counts[g["label"]] = counts.get(g["label"], 0) + 1
            if all(c == 1 for c in counts.values()):
                break
            depth += 1


# ---------------------------------------------------------------------------
# 仓库子树装配
# ---------------------------------------------------------------------------


def _session_time(session: dict) -> float:
    return float(session.get("last_active") or session.get("started_at") or 0)


def _build_repos(sessions: list[dict], resolve: Optional[Resolve], hydrate: bool) -> list[dict]:
    """为一组会话构建 ``repo → lane → sessions`` 子树。"""
    lanes: dict[str, dict] = {}  # lane_key -> {group, repo_key, repo_label, repo_path}

    for session in sessions:
        cwd = (session.get("cwd") or "").strip()
        if not cwd:
            continue

        placement = _place(
            cwd,
            (session.get("git_branch") or "").strip(),
            resolve,
            (session.get("git_repo_root") or "").strip(),
        )
        if not placement:
            continue

        lane_identity = _lane_key(placement["lane_key"])
        entry = lanes.get(lane_identity)
        if entry is None:
            entry = {
                "group": {
                    "id": placement["lane_key"],
                    "label": placement["lane_label"],
                    "path": placement["lane_path"],
                    "isMain": placement["is_main"],
                    "isKanban": placement["is_kanban"],
                    "sessions": [],
                },
                "repo_key": placement["repo_key"],
                "repo_label": placement["repo_label"],
                "repo_path": placement["repo_path"],
            }
            lanes[lane_identity] = entry
        entry["group"]["sessions"].append(session)

    repos: dict[str, dict] = {}
    for entry in lanes.values():
        group = entry["group"]
        group["sessions"].sort(key=_session_time, reverse=True)
        count = len(group["sessions"])
        if not hydrate:
            group["sessions"] = []

        repo_identity = _path_key(entry["repo_key"])
        repo = repos.get(repo_identity)
        if repo is None:
            repo = {
                "id": entry["repo_key"],
                "label": entry["repo_label"],
                "path": entry["repo_path"],
                "groups": [],
                "sessionCount": 0,
            }
            repos[repo_identity] = repo
        repo["groups"].append(group)
        repo["sessionCount"] += count

    repo_list = list(repos.values())
    for repo in repo_list:
        repo["groups"] = _sort_lanes(repo["groups"])
        _disambiguate_labels(repo["groups"])
    _disambiguate_labels(repo_list)
    return repo_list


def _seed_folder_repos(
    repos: list[dict], folders: list[dict], resolve: Optional[Resolve]
) -> list[dict]:
    """确保每个声明的项目文件夹都显示为仓库，即便 0 会话。

    全新项目（或会话尚未加载的项目）的会话派生 ``repos`` 列表为空。这在桌面端破坏两件事：
    进入项目视图渲染空白（无仓库时提前返回），且乐观实时会话覆盖层没有泳道可放新建会话——
    故项目里的新会话只在整树刷新后才出现。把每个文件夹播种为空仓库修复二者：覆盖层按新会话
    cwd 在文件夹根下匹配，drill-in 渲染真实（即便空）的项目主体。已被会话派生仓库（同 git 根）
    覆盖的文件夹保持不动。
    """
    seen = {
        _path_key(value)
        for repo in repos
        for value in (repo.get("id"), repo.get("path"))
        if value
    }
    seeded = list(repos)

    for folder in folders or []:
        raw = (folder.get("path") or "").strip()
        if not raw:
            continue
        info = resolve(raw) if resolve else None
        root = (info or {}).get("repo_root") or re.sub(r"[/\\]+$", "", raw)
        root_key = _path_key(root)
        if not root_key or root_key in seen:
            continue
        seeded.append({"id": root, "label": base_name(root) or root, "path": root, "groups": [], "sessionCount": 0})
        seen.add(root_key)

    if len(seeded) != len(repos):
        _disambiguate_labels(seeded)

    return seeded


# ---------------------------------------------------------------------------
# 显式项目归属
# ---------------------------------------------------------------------------


class _FolderIndex:
    """把规范化文件夹路径映射到 (归属项目, 深度)，使会话经遍历其 cwd 的祖先匹配到项目
    （O(路径深度) 次 dict 查找），而非每会话扫描所有 项目 × 文件夹——在重度用户规模下这是
    O(会话 × 项目) 与 O(会话) 的差别。
    """

    def __init__(self, projects: list[dict]) -> None:
        self._by_path: dict[str, tuple[dict, int]] = {}
        for project in projects:
            for folder in project.get("folders") or []:
                segs = _comparison_segments(folder.get("path") or "")
                if not segs:
                    continue
                key = "/".join(segs)
                depth = len(segs)
                # 最深文件夹胜出；平局保留首个项目（扫描顺序）。
                existing = self._by_path.get(key)
                if existing is None or depth > existing[1]:
                    self._by_path[key] = (project, depth)

    def match(self, target: str) -> tuple[Optional[dict], int]:
        """``target`` 的归属项目（按最长祖先文件夹），+ 其深度。"""
        segs = _comparison_segments(target or "")
        # 最长前缀优先 → 最深（最具体）文件夹胜出。
        for end in range(len(segs), 0, -1):
            hit = self._by_path.get("/".join(segs[:end]))
            if hit:
                return hit
        return None, -1


def _project_for_path(index: _FolderIndex, target: str) -> Optional[dict]:
    return index.match(target)[0]


def _project_for_session(session: dict, index: _FolderIndex, resolve: Optional[Resolve]) -> Optional[dict]:
    cwd = (session.get("cwd") or "").strip()
    if not cwd:
        return None
    repo_root = _session_repo_root(session, resolve)
    candidates = [cwd, repo_root] if repo_root and repo_root != cwd else [cwd]

    best: Optional[dict] = None
    best_len = -1
    for target in candidates:
        match, length = index.match(target)
        if match and length > best_len:
            best_len = length
            best = match
    return best


# ---------------------------------------------------------------------------
# 公共构建器
# ---------------------------------------------------------------------------


def _project_node(
    *,
    pid: str,
    label: str,
    path: Optional[str],
    repos: list[dict],
    session_count: int,
    last_active: float,
    preview_sessions: list[dict],
    color: Any = None,
    icon: Any = None,
    is_auto: bool = False,
) -> dict:
    return {
        "id": pid,
        "label": label,
        "path": path,
        "color": color,
        "icon": icon,
        "isAuto": is_auto,
        "sessionCount": session_count,
        "lastActive": last_active,
        "repos": repos,
        "previewSessions": preview_sessions,
    }


def build_tree(
    projects: list[dict],
    sessions: list[dict],
    discovered_repos: list[dict],
    resolve: Optional[Resolve] = None,
    *,
    preview_limit: int = 3,
    hydrate: bool = False,
    is_junk_root: Optional[Callable[[str], bool]] = None,
    is_junk_cwd: Optional[Callable[[str], bool]] = None,
) -> dict:
    """构建权威项目树。

    ``projects`` 是 ``projects_db.Project.to_dict()`` 形状（非归档）。``sessions`` 是投影的会话行
    dict（须带 ``id``、``cwd``、``git_branch``、``git_repo_root``、``started_at``、``last_active``）。
    ``discovered_repos`` 是 ``{"root", "label", "sessions", "last_active"}``。``is_junk_root`` 标记
    绝不应成为 AUTO 项目的 git 根（裸 home 目录、SPIRIT_HOME 子树）。``is_junk_cwd`` 是针对非 git
    会话文件夹的更窄策略：即便父树含 Spirit 状态，选定的后代仍可能是有意的工作区。用户创建的
    项目无论如何都被尊重。

    返回 ``{"projects": [...], "scoped_session_ids": [...]}``。``hydrate`` 为 False（概览）时泳道
    ``sessions`` 数组被清空但每个计数保留，且每个项目携带至多 ``preview_limit`` 个
    ``previewSessions``。为 True（drill-in）时泳道携带完整会话行。
    """
    active_projects = [p for p in projects if not p.get("archived")]
    _junk = is_junk_root or (lambda _root: False)
    _junk_cwd = is_junk_cwd or (lambda _cwd: False)
    folder_index = _FolderIndex(active_projects)

    by_project: dict[str, list[dict]] = {}
    unowned: list[dict] = []
    for session in sessions:
        owner = _project_for_session(session, folder_index, resolve)
        if owner:
            by_project.setdefault(owner["id"], []).append(session)
        else:
            unowned.append(session)

    scoped_ids: list[str] = []

    def _previews(project_sessions: list[dict]) -> list[dict]:
        if preview_limit <= 0:
            return []
        ordered = sorted(project_sessions, key=_session_time, reverse=True)
        return ordered[:preview_limit]

    def _last_active(project_sessions: list[dict]) -> float:
        return max((_session_time(s) for s in project_sessions), default=0.0)

    result: list[dict] = []

    # Tier 1：显式、用户创建的项目（总显示，即便 0 会话）。
    for project in active_projects:
        psessions = by_project.get(project["id"], [])
        scoped_ids.extend(s["id"] for s in psessions if s.get("id"))
        repos = _seed_folder_repos(
            _build_repos(psessions, resolve, hydrate), project.get("folders") or [], resolve
        )
        result.append(
            _project_node(
                pid=project["id"],
                label=project.get("name") or project["id"],
                path=project.get("primary_path"),
                color=project.get("color"),
                icon=project.get("icon"),
                repos=repos,
                session_count=len(psessions),
                last_active=_last_active(psessions),
                preview_sessions=_previews(psessions),
            )
        )

    # Tier 2：从剩余会话生成自动项目。优先公共 git 仓库根，再回退到会话 cwd（历史/非 git 工作区）。
    # 前 Projects 桌面把每个非空 cwd 都分组；保留该回退防止升级把这些会话压平进 Recents。
    by_auto_root: dict[str, dict] = {}

    def _add_auto(root: str, session: dict) -> None:
        key = _path_key(root)
        if not key:
            return
        bucket = by_auto_root.setdefault(key, {"root": root, "sessions": []})
        bucket["sessions"].append(session)

    for session in unowned:
        root = _session_repo_root(session, resolve)
        if root:
            # 真实 git 根用更严的仓库策略。不把被过滤的内部仓库重解释为纯 cwd 项目。
            if not _junk(root):
                _add_auto(root, session)
            continue

        cwd = (session.get("cwd") or "").strip()
        if not cwd or _junk_cwd(cwd):
            continue
        placement = _place(
            cwd,
            (session.get("git_branch") or "").strip(),
            resolve,
            (session.get("git_repo_root") or "").strip(),
        )
        if placement:
            _add_auto(placement["repo_key"], session)

    seen: set[str] = set()
    for bucket in by_auto_root.values():
        auto_root = bucket["root"]
        auto_sessions = bucket["sessions"]
        auto_key = _path_key(auto_root)
        repos = _build_repos(auto_sessions, resolve, hydrate)
        repo_node = next(
            (
                repo
                for repo in repos
                if _path_key(repo.get("id") or repo.get("path") or "") == auto_key
            ),
            None,
        )
        if repo_node is None:
            continue
        seen.add(auto_key)
        scoped_ids.extend(s["id"] for s in auto_sessions if s.get("id"))
        result.append(
            _project_node(
                pid=auto_root,
                label=base_name(auto_root) or auto_root,
                path=auto_root,
                repos=repos,
                session_count=repo_node["sessionCount"],
                last_active=_last_active(auto_sessions),
                preview_sessions=_previews(auto_sessions),
                is_auto=True,
            )
        )

    # Tier 3：从完整历史 / 磁盘扫描发现、无已加载会话、折叠到公共根且不被显式项目拥有的仓库。
    for repo in discovered_repos or []:
        raw_root = (repo.get("root") or "").strip()
        if not raw_root:
            continue
        info = resolve(raw_root) if resolve else None
        root = (info or {}).get("repo_root") or raw_root
        root_key = _path_key(root)
        if root_key in seen or _junk(root) or _project_for_path(folder_index, root):
            continue
        seen.add(root_key)
        label = repo.get("label") or base_name(root) or root
        result.append(
            _project_node(
                pid=root,
                label=label,
                path=root,
                repos=[{"id": root, "label": label, "path": root, "groups": [], "sessionCount": 0}],
                session_count=int(repo.get("sessions") or 0),
                last_active=float(repo.get("last_active") or 0),
                preview_sessions=[],
                is_auto=True,
            )
        )

    # 自动项目按仓库 basename 打标签，可能冲突（不同父目录下两个 "app" 仓库）。长路径前缀使各自
    # 唯一。显式项目保留用户选定的名字不动。
    _disambiguate_labels([p for p in result if p.get("isAuto")])

    return {"projects": result, "scoped_session_ids": scoped_ids}


__all__ = [
    "DEFAULT_BRANCH_LABEL",
    "Resolve",
    "base_name",
    "build_tree",
    "kanban_worktree_dir",
]
