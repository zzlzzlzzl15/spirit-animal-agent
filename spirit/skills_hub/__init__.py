"""技能中心（Skills Hub）子系统 — Spirit Agent（Phase 4.6）。

对标 Hermes 的 ``agent/skill_commands.py`` + ``agent/skill_bundles.py`` +
``agent/skill_preprocessing.py`` + ``agent/skill_utils.py`` + ``agent/skill_provenance.py``
+ ``agent/skill_usage.py`` + ``tools/skills_hub.py``，按 Spirit「功能域合并 + 自注册」
约定收拢为一个内聚包。模块划分：

- :mod:`spirit.skills_hub.paths` — 按调用解析的目录约定（``SPIRIT_HOME`` / env 覆盖，
  测试可 monkeypatch 重定向）。
- :mod:`spirit.skills_hub.discovery` — 技能发现单一事实源（frontmatter 解析、平台 /
  环境 / 禁用三层门、外部目录、索引遍历、``SkillMeta``）。
- :mod:`spirit.skills_hub.preprocessing` — 模板变量替换 + 内联 shell 展开。
- :mod:`spirit.skills_hub.usage` — 使用 / 查看计数与活跃度聚合。
- :mod:`spirit.skills_hub.provenance` — 写入来源 ContextVar + 安装来源记录。
- :mod:`spirit.skills_hub.commands` — 技能 slash 命令扫描 / 解析 / 调用消息构建（含
  堆叠调用与会话级预加载）。
- :mod:`spirit.skills_hub.bundles` — 捆绑包（命名一组技能，``/<bundle>`` 一次载 N 个）。
- :mod:`spirit.skills_hub.hub` — 技能市场（获取 seam → 隔离 → 扫描 → 安装 / 卸载 +
  锁文件 / 审计 / 来源 / 索引缓存）。
- :mod:`spirit.skills_hub.dispatch` — 传输无关的 ``/skill`` 命令分发 + 动态
  ``/<skill-name>`` · ``/<bundle>`` 解析。

``spirit.tools.skills`` 的安全扫描器（Skills Guard）仍留在工具层（被 :mod:`hub` 延迟
导入用于安装前扫描），本包专注发现 / 调用 / 市场 / 命令派发。
"""

from __future__ import annotations

# ── 路径约定 ──────────────────────────────────────────────────────────
from spirit.skills_hub.paths import (
    EXCLUDED_SKILL_DIRS,
    SKILL_SUPPORT_DIRS,
    audit_log,
    bundles_dir,
    hub_dir,
    index_cache_dir,
    lock_file,
    provenance_file,
    quarantine_dir,
    skills_dir,
    spirit_home,
    taps_file,
    usage_file,
)

# ── 发现（单一事实源） ────────────────────────────────────────────────
from spirit.skills_hub.discovery import (
    PLATFORM_MAP,
    SkillMeta,
    clear_env_cache,
    find_all_skills,
    get_all_skills_dirs,
    get_disabled_skill_names,
    get_external_skills_dirs,
    is_excluded_skill_path,
    is_skill_support_path,
    iter_skill_index_files,
    normalize_skill_lookup_name,
    parse_frontmatter,
    parse_skill_md,
    skill_matches_environment,
    skill_matches_platform,
    skill_matches_platform_list,
    yaml_load,
)

# ── 预处理 ────────────────────────────────────────────────────────────
from spirit.skills_hub.preprocessing import (
    expand_inline_shell,
    preprocess_skill_content,
    run_inline_shell,
    substitute_template_vars,
)

# ── 使用统计 ──────────────────────────────────────────────────────────
from spirit.skills_hub.usage import (
    activity_count,
    all_usage,
    bump_use,
    bump_view,
    get_usage,
    latest_activity_at,
)

# ── 来源追踪 ──────────────────────────────────────────────────────────
from spirit.skills_hub.provenance import (
    BACKGROUND_REVIEW,
    FOREGROUND,
    all_installs,
    clear_install,
    get_current_write_origin,
    get_install,
    is_background_review,
    record_install,
    reset_current_write_origin,
    set_current_write_origin,
)

# ── slash 命令扫描 / 调用消息 ─────────────────────────────────────────
from spirit.skills_hub.commands import (
    build_preloaded_skills_prompt,
    build_skill_invocation_message,
    build_stacked_skill_invocation_message,
    extract_user_instruction_from_skill_message,
    get_skill_commands,
    invalidate_skill_commands,
    reload_skills,
    resolve_skill_command_key,
    scan_skill_commands,
    slugify,
    split_stacked_skill_commands,
)

# ── 捆绑包 ────────────────────────────────────────────────────────────
from spirit.skills_hub.bundles import (
    build_bundle_invocation_message,
    bundle_path_for,
    delete_bundle,
    get_bundle,
    get_skill_bundles,
    invalidate_bundles,
    list_bundles,
    reload_bundles,
    resolve_bundle_command_key,
    save_bundle,
    scan_bundles,
)

# ── 市场（Hub） ───────────────────────────────────────────────────────
from spirit.skills_hub.hub import (
    HubError,
    HubLockFile,
    HubSourceUnavailable,
    SkillBundle,
    TapsManager,
    append_audit_log,
    browse_index,
    bundle_content_hash,
    content_hash,
    ensure_hub_dirs,
    fetch_bundle,
    install_bundle,
    install_from_quarantine,
    install_skill,
    quarantine_bundle,
    read_audit_log,
    read_index_cache,
    register_index_source,
    register_source,
    search_skills,
    set_fetcher,
    uninstall_skill,
    write_index_cache,
)

# ── 传输无关命令分发 ──────────────────────────────────────────────────
from spirit.skills_hub.dispatch import (
    RESERVED_SKILL_COMMANDS,
    handle_skill_command,
    resolve_slash_skill_or_bundle,
    skill_usage,
)

__all__ = [
    # paths
    "EXCLUDED_SKILL_DIRS",
    "SKILL_SUPPORT_DIRS",
    "spirit_home",
    "skills_dir",
    "hub_dir",
    "lock_file",
    "quarantine_dir",
    "audit_log",
    "taps_file",
    "index_cache_dir",
    "usage_file",
    "provenance_file",
    "bundles_dir",
    # discovery
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
    # preprocessing
    "substitute_template_vars",
    "run_inline_shell",
    "expand_inline_shell",
    "preprocess_skill_content",
    # usage
    "bump_use",
    "bump_view",
    "get_usage",
    "all_usage",
    "activity_count",
    "latest_activity_at",
    # provenance
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "set_current_write_origin",
    "reset_current_write_origin",
    "get_current_write_origin",
    "is_background_review",
    "record_install",
    "clear_install",
    "get_install",
    "all_installs",
    # commands
    "slugify",
    "scan_skill_commands",
    "get_skill_commands",
    "invalidate_skill_commands",
    "reload_skills",
    "resolve_skill_command_key",
    "build_skill_invocation_message",
    "split_stacked_skill_commands",
    "build_stacked_skill_invocation_message",
    "build_preloaded_skills_prompt",
    "extract_user_instruction_from_skill_message",
    # bundles
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
    # hub
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
    # dispatch
    "RESERVED_SKILL_COMMANDS",
    "handle_skill_command",
    "skill_usage",
    "resolve_slash_skill_or_bundle",
]
