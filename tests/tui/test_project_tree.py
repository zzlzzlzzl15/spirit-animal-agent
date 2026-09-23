"""权威项目树构建器（``spirit.tui.project_tree``）的不变量测试（Phase 4.4）。

对标 Hermes ``tests/tui_gateway/test_project_tree.py``（逐字移植，仅改 import）：断言结构契约
（worktree 折叠、kanban 塌缩、泳道 id 方案、成员并集、Windows 路径身份、标签消歧、概览 vs
drill-in），而非快照，故常规数据变动不会打破它们。project_tree 是纯 stdlib 模块，git 解析经
``resolve`` seam 注入，全部离线。
"""

from __future__ import annotations

from spirit.tui import project_tree as pt

_SID = 0


def _session(cwd, *, branch="", repo_root="", **over):
    global _SID
    _SID += 1
    row = {
        "id": f"s{_SID}",
        "cwd": cwd,
        "git_branch": branch,
        "git_repo_root": repo_root,
        "started_at": 1000,
        "last_active": 1000,
        "title": None,
        "preview": None,
        "source": "cli",
    }
    row.update(over)
    return row


def _project(pid, name, folders, **over):
    row = {
        "id": pid,
        "name": name,
        "primary_path": folders[0] if folders else None,
        "archived": False,
        "folders": [{"path": p, "is_primary": i == 0} for i, p in enumerate(folders)],
    }
    row.update(over)
    return row


def _resolver(mapping):
    """从 {cwd: (repo_root, worktree_root)} 造一个 resolve()。"""

    def resolve(cwd):
        hit = mapping.get(cwd)
        if not hit:
            return None
        return {"repo_root": hit[0], "worktree_root": hit[1]}

    return resolve


def _lane_ids(project):
    return [g["id"] for repo in project["repos"] for g in repo["groups"]]


# ---------------------------------------------------------------------------


def test_main_checkout_groups_by_recorded_branch_with_stable_lane_ids():
    resolve = _resolver({"/repo": ("/repo", "/repo")})
    sessions = [
        _session("/repo", branch="main"),
        _session("/repo", branch="feature"),
    ]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)
    project = next(p for p in tree["projects"] if p["id"] == "/repo")

    assert project["isAuto"] is True
    assert _lane_ids(project) == ["/repo::branch::main", "/repo::branch::feature"]
    # trunk 排在 feature 分支之前；两者都在主检出里。
    assert [g["label"] for repo in project["repos"] for g in repo["groups"]] == ["main", "feature"]
    assert all(g["isMain"] for repo in project["repos"] for g in repo["groups"])


def test_linked_worktrees_fold_under_their_common_repo_root():
    # 链接 worktree 自己的 toplevel 是 /elsewhere/wt，但其公共根是 /repo，故必须分组到 /repo 下
    # （而非独立项目）。
    resolve = _resolver(
        {
            "/repo": ("/repo", "/repo"),
            "/elsewhere/wt": ("/repo", "/elsewhere/wt"),
        }
    )
    sessions = [
        _session("/repo", branch="main"),
        _session("/elsewhere/wt", branch="feature"),
    ]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)

    assert [p["id"] for p in tree["projects"]] == ["/repo"]
    project = tree["projects"][0]
    assert project["repos"][0]["id"] == "/repo"
    lane_ids = _lane_ids(project)
    assert "/repo::branch::main" in lane_ids
    # 链接 worktree 泳道以 worktree 路径为键，且非 main。
    linked = next(g for repo in project["repos"] for g in repo["groups"] if not g["isMain"])
    assert linked["id"] == "/elsewhere/wt"
    assert linked["path"] == "/elsewhere/wt"


def test_kanban_task_worktrees_collapse_into_one_bucket():
    resolve = _resolver(
        {
            "/repo": ("/repo", "/repo"),
            "/repo/.worktrees/t_aaaaaaaa": ("/repo", "/repo/.worktrees/t_aaaaaaaa"),
            "/repo/.worktrees/t_bbbbbbbb": ("/repo", "/repo/.worktrees/t_bbbbbbbb"),
        }
    )
    sessions = [
        _session("/repo", branch="main"),
        _session("/repo/.worktrees/t_aaaaaaaa"),
        _session("/repo/.worktrees/t_bbbbbbbb"),
    ]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)
    project = tree["projects"][0]
    kanban = [g for repo in project["repos"] for g in repo["groups"] if g.get("isKanban")]

    assert len(kanban) == 1
    assert kanban[0]["id"] == "/repo::kanban"
    assert kanban[0]["path"] == "/repo/.worktrees"
    assert len(kanban[0]["sessions"]) == 2
    # 桶排在真实主分支之下。
    assert _lane_ids(project)[-1] == "/repo::kanban"


def test_user_worktree_under_dotworktrees_is_its_own_lane_not_kanban():
    # 用户 "New worktree" 位于 <repo>/.worktrees/<slug>（无 t_ id），故不得塌缩进 kanban 桶——
    # 它获得自己的链接泳道。
    resolve = _resolver(
        {
            "/repo": ("/repo", "/repo"),
            "/repo/.worktrees/test-gui-stuff": ("/repo", "/repo/.worktrees/test-gui-stuff"),
        }
    )
    sessions = [
        _session("/repo", branch="main"),
        _session("/repo/.worktrees/test-gui-stuff", branch="hermes/test-gui-stuff"),
    ]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)
    project = tree["projects"][0]
    lanes = {g["id"]: g for repo in project["repos"] for g in repo["groups"]}

    assert "/repo/.worktrees/test-gui-stuff" in lanes
    assert not lanes["/repo/.worktrees/test-gui-stuff"].get("isKanban")
    assert "/repo::kanban" not in lanes


def test_unrecorded_and_recorded_main_share_one_lane():
    # 空 git_branch（历史会话）折叠进与记录分支 "main" 相同的 trunk 泳道——无重复 "main"。
    resolve = _resolver({"/repo": ("/repo", "/repo")})
    sessions = [_session("/repo", branch=""), _session("/repo", branch="main")]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)
    project = tree["projects"][0]
    main_lanes = [g for repo in project["repos"] for g in repo["groups"] if g["label"] == "main"]

    assert len(main_lanes) == 1
    assert main_lanes[0]["id"] == "/repo::branch::main"
    assert len(main_lanes[0]["sessions"]) == 2


def test_persisted_repo_root_used_when_no_live_probe():
    # 无解析器（远程后端）：回退到持久化的 git_repo_root，并按会话记录的分支切分主检出。
    sessions = [_session("/repo/src", branch="main", repo_root="/repo")]

    tree = pt.build_tree([], sessions, [], resolve=None, hydrate=True)
    project = next(p for p in tree["projects"] if p["id"] == "/repo")

    assert _lane_ids(project) == ["/repo::branch::main"]


def test_non_git_cwd_preserves_legacy_workspace_grouping():
    # 一等 Projects 之前，每个非空会话 cwd 都作为工作区出现，即便它不是 git 仓库。历史会话必须
    # 保留该分组，而非落入扁平 Sessions 列表。
    legacy = _session("/work/notes", title="Research notes")

    tree = pt.build_tree([], [legacy], [], resolve=lambda _cwd: None, hydrate=True)

    assert [p["id"] for p in tree["projects"]] == ["/work/notes"]
    project = tree["projects"][0]
    assert project["isAuto"] is True
    assert project["label"] == "notes"
    assert project["sessionCount"] == 1
    assert _lane_ids(project) == ["/work/notes"]
    assert tree["scoped_session_ids"] == [legacy["id"]]


def test_non_git_windows_cwd_preserves_legacy_workspace_grouping():
    cwd = r"C:\Users\alice\workspace\notes"
    legacy = _session(cwd)

    tree = pt.build_tree([], [legacy], [], resolve=lambda _cwd: None, hydrate=True)

    assert [p["id"] for p in tree["projects"]] == [cwd]
    assert tree["projects"][0]["label"] == "notes"
    assert tree["scoped_session_ids"] == [legacy["id"]]


def test_equivalent_windows_cwds_collapse_into_one_auto_project():
    sessions = [
        _session("C:/work/notes"),
        _session(r"c:\WORK\notes"),
        _session("C:/work/notes/"),
    ]

    tree = pt.build_tree([], sessions, [], resolve=lambda _cwd: None, hydrate=True)

    assert len(tree["projects"]) == 1
    project = tree["projects"][0]
    assert project["id"] == "C:/work/notes"
    assert project["sessionCount"] == 3
    assert len(project["repos"]) == 1
    assert len(project["repos"][0]["groups"]) == 1
    assert len(project["repos"][0]["groups"][0]["sessions"]) == 3


def test_windows_path_identity_preserves_explicit_project_priority():
    explicit = _project("p_notes", "Notes", ["C:/Work/Notes"])
    session = _session("c:\\work\\notes\\")

    tree = pt.build_tree([explicit], [session], [], resolve=lambda _cwd: None, hydrate=True)

    assert [p["id"] for p in tree["projects"]] == ["p_notes"]
    assert tree["projects"][0]["sessionCount"] == 1
    assert tree["scoped_session_ids"] == [session["id"]]


def test_wsl_localhost_cwds_collapse_into_one_auto_project():
    # 根相对 WSL 拼写（单前导反斜杠）是 Windows 路径，故大小写/分隔符变体塌缩而非派生重复 auto。
    sessions = [
        _session(r"\wsl.localhost\Ubuntu\home\alice\proj"),
        _session("//wsl.localhost/Ubuntu/home/alice/PROJ"),
    ]

    tree = pt.build_tree([], sessions, [], resolve=lambda _cwd: None, hydrate=True)

    assert len(tree["projects"]) == 1
    assert tree["projects"][0]["sessionCount"] == 2


def test_wsl_localhost_path_cannot_bypass_explicit_project():
    explicit = _project("p_proj", "Proj", [r"\wsl.localhost\Ubuntu\home\alice\proj"])
    session = _session("//wsl.localhost/Ubuntu/home/alice/PROJ/")

    tree = pt.build_tree([explicit], [session], [], resolve=lambda _cwd: None, hydrate=True)

    assert [p["id"] for p in tree["projects"]] == ["p_proj"]
    assert tree["projects"][0]["sessionCount"] == 1
    assert tree["scoped_session_ids"] == [session["id"]]


def test_posix_path_identity_remains_case_sensitive():
    explicit = _project("p_notes", "Notes", ["/Work/Notes"])
    session = _session("/work/notes")

    tree = pt.build_tree([explicit], [session], [], resolve=lambda _cwd: None, hydrate=True)

    assert [(p["id"], p["sessionCount"]) for p in tree["projects"]] == [
        ("p_notes", 0),
        ("/work/notes", 1),
    ]


def test_explicit_project_claims_sessions_and_beats_auto():
    project = _project("p_app", "App", ["/www/app"])
    resolve = _resolver(
        {
            "/www/app": ("/www/app", "/www/app"),
            "/www/other": ("/www/other", "/www/other"),
        }
    )
    sessions = [
        _session("/www/app", branch="main"),
        _session("/www/other", branch="main"),
    ]

    tree = pt.build_tree([project], sessions, [], resolve, hydrate=True)

    explicit = next(p for p in tree["projects"] if p["id"] == "p_app")
    assert explicit["isAuto"] is False
    assert explicit["sessionCount"] == 1
    # 无归属的 /www/other 会话成为自己的 auto 项目。
    assert any(p["id"] == "/www/other" and p["isAuto"] for p in tree["projects"])


def test_scoped_session_ids_is_union_of_placed_sessions():
    project = _project("p_app", "App", ["/www/app"])
    resolve = _resolver(
        {
            "/www/app": ("/www/app", "/www/app"),
            "/www/repo": ("/www/repo", "/www/repo"),
        }
    )
    owned = _session("/www/app", branch="main")
    auto = _session("/www/repo", branch="main")
    homeless = _session(None)  # 无 cwd -> 不属于任何项目

    tree = pt.build_tree([project], [owned, auto, homeless], [], resolve, hydrate=True)

    assert set(tree["scoped_session_ids"]) == {owned["id"], auto["id"]}
    assert homeless["id"] not in tree["scoped_session_ids"]


def test_overview_drops_session_rows_but_keeps_counts_and_previews():
    resolve = _resolver({"/repo": ("/repo", "/repo")})
    sessions = [_session("/repo", branch="main") for _ in range(4)]

    tree = pt.build_tree([], sessions, [], resolve, preview_limit=3, hydrate=False)
    project = tree["projects"][0]

    assert project["sessionCount"] == 4
    assert len(project["previewSessions"]) == 3
    # 概览模式下泳道携带结构 + 计数但无行。
    assert all(g["sessions"] == [] for repo in project["repos"] for g in repo["groups"])
    assert project["repos"][0]["sessionCount"] == 4


def test_discovered_repo_with_no_sessions_becomes_zero_session_project():
    discovered = [{"root": "/www/fresh", "label": "fresh", "sessions": 0, "last_active": 5}]

    tree = pt.build_tree([], [], discovered, resolve=None, hydrate=False)

    fresh = next(p for p in tree["projects"] if p["id"] == "/www/fresh")
    assert fresh["isAuto"] is True
    assert fresh["sessionCount"] == 0
    assert fresh["repos"][0]["groups"] == []


def test_explicit_project_with_no_sessions_seeds_its_folders_as_repos():
    # 全新（或未加载）项目仍须把其声明的文件夹暴露为仓库，使进入视图能渲染、且桌面乐观覆盖层
    # 有泳道可放新建会话（否则只在整树刷新后才出现）。
    project = _project("p_new", "New", ["/work/blank"])

    tree = pt.build_tree([project], [], [], resolve=None, hydrate=True)

    node = next(p for p in tree["projects"] if p["id"] == "p_new")
    assert node["sessionCount"] == 0
    assert [r["path"] for r in node["repos"]] == ["/work/blank"]
    assert node["repos"][0]["groups"] == []


def test_seeded_folder_repo_does_not_duplicate_a_session_derived_repo():
    # 当文件夹已有会话（同 git 根）时，播种不得为同路径添加第二个仓库。
    project = _project("p_app", "App", ["/www/app"])
    resolve = _resolver({"/www/app": ("/www/app", "/www/app")})
    sessions = [_session("/www/app", branch="main")]

    tree = pt.build_tree([project], sessions, [], resolve, hydrate=True)

    node = next(p for p in tree["projects"] if p["id"] == "p_app")
    assert [r["path"] for r in node["repos"]] == ["/www/app"]


def test_discovered_repo_owned_by_explicit_project_is_not_duplicated():
    project = _project("p_app", "App", ["/www/app"])
    discovered = [{"root": "/www/app", "label": "app", "sessions": 2, "last_active": 1}]

    tree = pt.build_tree([project], [], discovered, resolve=None, hydrate=False)

    assert [p["id"] for p in tree["projects"] if p["path"] == "/www/app"] == ["p_app"]


def test_nested_project_folders_pick_the_deepest_match():
    # 文件夹索引必须把会话解析到其最具体（最深）的项目文件夹，而非任意祖先。
    outer = _project("p_outer", "Outer", ["/work"])
    inner = _project("p_inner", "Inner", ["/work/app"])
    resolve = _resolver(
        {
            "/work/app": ("/work/app", "/work/app"),
            "/work/other": ("/work/other", "/work/other"),
        }
    )

    tree = pt.build_tree(
        [outer, inner],
        [_session("/work/app", branch="main"), _session("/work/other", branch="main")],
        [],
        resolve,
        hydrate=True,
    )
    by_id = {p["id"]: p for p in tree["projects"]}

    assert by_id["p_inner"]["sessionCount"] == 1  # /work/app → 最深文件夹胜出
    assert by_id["p_outer"]["sessionCount"] == 1  # /work/other → 只有外层项目


def test_junk_root_never_becomes_an_auto_project():
    # git 根为 SPIRIT_HOME（配置/状态）的会话不得派生幻影项目；它落入扁平 Recents（未 scoped）。
    # 旁边的真实仓库仍正常分组。
    resolve = _resolver(
        {
            "/home/me/.spirit": ("/home/me/.spirit", "/home/me/.spirit"),
            "/www/app": ("/www/app", "/www/app"),
        }
    )
    junk = _session("/home/me/.spirit", branch="main")
    real = _session("/www/app", branch="main")
    is_junk = lambda root: root == "/home/me/.spirit"

    tree = pt.build_tree([], [junk, real], [], resolve, hydrate=True, is_junk_root=is_junk)

    ids = {p["id"] for p in tree["projects"]}
    assert ids == {"/www/app"}
    assert junk["id"] not in tree["scoped_session_ids"]
    assert real["id"] in tree["scoped_session_ids"]


def test_junk_root_is_dropped_from_the_discovered_tier():
    discovered = [{"root": "/home/me/.spirit", "label": ".spirit", "sessions": 0, "last_active": 9}]

    tree = pt.build_tree([], [], discovered, resolve=None, is_junk_root=lambda r: r == "/home/me/.spirit")

    assert tree["projects"] == []


def test_non_git_cwd_can_group_inside_a_junk_repo_subtree():
    # 仓库发现拒绝整个状态子树，但选定的非 git 后代可能是从旧 UI 承接的有意工作区。
    workspace = _session("/home/test/.spirit/workspaces/notes")

    tree = pt.build_tree(
        [],
        [workspace],
        [],
        resolve=lambda _cwd: None,
        hydrate=True,
        is_junk_root=lambda path: path.startswith("/home/test/.spirit"),
        is_junk_cwd=lambda path: path in {"/home/test", "/home/test/.spirit"},
    )

    assert [p["id"] for p in tree["projects"]] == ["/home/test/.spirit/workspaces/notes"]
    assert tree["scoped_session_ids"] == [workspace["id"]]


def test_broad_default_non_git_cwd_stays_unscoped():
    detached = _session("/home/test/.spirit")

    tree = pt.build_tree(
        [],
        [detached],
        [],
        resolve=lambda _cwd: None,
        hydrate=True,
        is_junk_cwd=lambda path: path in {"/home/test", "/home/test/.spirit"},
    )

    assert tree["projects"] == []
    assert detached["id"] not in tree["scoped_session_ids"]


def test_colliding_repo_basenames_disambiguate_labels():
    resolve = _resolver(
        {
            "/x/proj": ("/x/proj", "/x/proj"),
            "/y/proj": ("/y/proj", "/y/proj"),
        }
    )
    sessions = [_session("/x/proj", branch="main"), _session("/y/proj", branch="main")]

    tree = pt.build_tree([], sessions, [], resolve, hydrate=True)
    labels = sorted(p["label"] for p in tree["projects"])

    assert labels == ["x/proj", "y/proj"]
