"""spirit.profile — 多实例 Profile 隔离（Phase 4.2）。

一个 profile 是一个**完全独立的 HOME 目录**（自带 config/.env/memories/sessions/
skills/logs/plans），让用户在同一台机器上并行运行多个互不干扰的 Spirit 身份
（如"工作"/"个人"/"金融研究"），各自独立的凭据、记忆、技能与会话历史。默认 profile
``default`` 就是 ``SPIRIT_HOME`` 本身（零迁移，向后兼容）。

对标 Hermes ``hermes_cli/profiles.py``（2225 行）+ ``profile_describer.py``（289 行）
的**核心子集**，按 Spirit「精简 + 可测试」约定落地。缓建（记录于 docs/07 Phase 4.2）：
wrapper 别名脚本（对 Windows 不友好）、gateway service 注册（Hermes 专属）、
Git 分发（``profile_distribution``，独立关注点）、export/import 归档。

公共 API::

    from spirit.profile import (
        list_profiles, create_profile, delete_profile, rename_profile,
        get_active_profile, set_active_profile, handle_profile_command,
        describe_profile, build_agent_llm_caller,
    )

    create_profile("work", clone_config=True, description="工作身份")
    set_active_profile("work")
    result = handle_profile_command(agent, "list")   # → 结构化 dict，CLI/ws 各自渲染

模块划分::

    paths.py      路径解析（default_root/profiles_root/profile_dir/resolve_env/apply）
    manager.py    ProfileInfo + 名字校验 + CRUD + profile.yaml meta + active + skill 计数
    describer.py  注入式 LLM 自动描述（优雅降级，绝不抛）
    commands.py   /profile 传输无关分发（CLI / ws_server 共用）
"""

from __future__ import annotations

from spirit.profile.paths import (
    active_profile_path,
    apply_active_profile,
    default_root,
    profile_dir,
    profiles_root,
    resolve_profile_env,
)
from spirit.profile.manager import (
    ProfileInfo,
    create_profile,
    delete_profile,
    get_active_profile,
    get_active_profile_name,
    get_profile_dir,
    list_profiles,
    normalize_profile_name,
    profile_exists,
    read_profile_meta,
    rename_profile,
    set_active_profile,
    validate_profile_name,
    write_profile_meta,
)
from spirit.profile.describer import (
    DescribeOutcome,
    describe_profile,
    list_describable_profiles,
)
from spirit.profile.commands import handle_profile_command

__all__ = [
    # 路径层
    "default_root",
    "profiles_root",
    "active_profile_path",
    "profile_dir",
    "resolve_profile_env",
    "apply_active_profile",
    # 管理器
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
    # 自动描述
    "DescribeOutcome",
    "describe_profile",
    "list_describable_profiles",
    # 命令分发（CLI / ws_server 共用）
    "handle_profile_command",
]
