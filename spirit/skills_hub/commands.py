"""技能 slash 命令派发 — Spirit Agent（Phase 4.6）。

对标 Hermes ``agent/skill_commands.py``：把 ``~/.spirit/skills/`` 下的技能映射为
``/<skill-name>`` slash 命令，并构建「调用某技能」时注入对话的用户消息。传输无关——
CLI / 网关 / 桌宠 WebSocket 都调这里的纯函数，各自渲染结果。

核心契约（对齐 Hermes）：

- ``scan_skill_commands()`` 扫描所有技能目录，施加平台 / 环境 / 禁用门，产出
  ``{"/slug": {name, description, skill_md_path, skill_dir}}``。slug 由技能名归一化
  （空格 / 下划线 → 连字符，剥非字母数字）。
- ``resolve_skill_command_key(command)`` 把用户输入映射到规范 ``/slug``（连字符 / 下划线
  互通，适配 Telegram bot 命令名不许连字符）。
- ``build_skill_invocation_message(cmd_key, user_instruction, task_id)`` 载入技能正文、
  施加预处理、注入技能目录 + 支持文件提示，返回完整用户消息（并 ``bump_use``）。
- **堆叠调用** ``/skill-a /skill-b do XYZ``：``split_stacked_skill_commands`` 消费前导
  ``/skill`` token（至多 ``_MAX_STACKED_SKILLS`` 个），``build_stacked_skill_invocation_message``
  把多个技能合并进一条消息（复用 bundle 脚手架标记，便于指令回收）。
- ``build_preloaded_skills_prompt(identifiers)`` 供会话级预加载（``spirit -s <skill>``），
  禁用技能视同缺失（绕过扫描期禁用过滤，故须在此重新施加禁用门）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from spirit.skills_hub import discovery, preprocessing, usage
from spirit.skills_hub.paths import SKILL_SUPPORT_DIRS

logger = logging.getLogger(__name__)

# slug 归一化（与 bundles 共用，保证名为 "Foo Bar" 的技能与捆绑包都解析到 "/foo-bar"）。
_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")

# 堆叠 slash-skill 调用的上限（对标 Hermes _MAX_STACKED_SKILLS，灵感来自 Claude Code）。
_MAX_STACKED_SKILLS = 5

# 扫描期命令缓存（模块级；scan_skill_commands 重置并重填）。
_skill_commands: Dict[str, Dict[str, Any]] = {}
_skill_commands_platform: Optional[str] = None


def slugify(name: str) -> str:
    """把技能名归一化为连字符分隔的 slug（剥非字母数字，折叠多连字符）。"""
    cmd = (name or "").lower().replace(" ", "-").replace("_", "-")
    cmd = _SKILL_INVALID_CHARS.sub("", cmd)
    cmd = _SKILL_MULTI_HYPHEN.sub("-", cmd).strip("-")
    return cmd


def _resolve_skill_commands_platform() -> Optional[str]:
    """当前命令作用域的平台（``SPIRIT_PLATFORM`` 环境变量；网关多平台进程会切）。"""
    return os.getenv("SPIRIT_PLATFORM") or None


# ---------------------------------------------------------------------------
# 技能载入
# ---------------------------------------------------------------------------

def _resolve_skill_dir(identifier: str) -> Optional[Path]:
    """把技能标识符（名 / 相对路径 / 归一化后的绝对路径）解析为技能目录。

    依次尝试：① 各技能根下的直接相对路径；② 按目录叶名 rglob；③ 按 frontmatter 名
    （含 slug 归一化）匹配。找不到返回 None。
    """
    ident = (identifier or "").strip().strip("/")
    if not ident:
        return None

    all_dirs = discovery.get_all_skills_dirs()

    # ① 直接相对路径
    for base in all_dirs:
        cand = base / ident
        if (cand / "SKILL.md").is_file():
            return cand

    leaf = PurePosixPath(ident.replace("\\", "/")).name

    # ② 按目录叶名
    for base in all_dirs:
        if not base.is_dir():
            continue
        for skill_md in discovery.iter_skill_index_files(base, "SKILL.md"):
            if skill_md.parent.name == leaf:
                return skill_md.parent

    # ③ 按 frontmatter 名（精确或 slug 相等）
    ident_slug = slugify(ident)
    for meta in discovery.find_all_skills():
        if meta.name == ident or (ident_slug and slugify(meta.name) == ident_slug):
            return Path(meta.path)

    return None


def _load_skill_payload(
    skill_identifier: str,
    task_id: Optional[str] = None,
) -> Optional[Tuple[Dict[str, Any], Optional[Path], str]]:
    """按名 / 路径载入技能，返回 ``(loaded_payload, skill_dir, display_name)``（找不到 None）。

    ``loaded_payload`` 形状（供 :func:`_build_skill_message` 消费）::

        success: True
        name:    frontmatter 名（回退目录名）
        content: frontmatter 之后的正文
        raw_content: 含 frontmatter 的全文
        skill_dir:   技能目录绝对路径（str）
        frontmatter: 解析后的 frontmatter dict
    """
    raw = (skill_identifier or "").strip()
    if not raw:
        return None
    normalized = discovery.normalize_skill_lookup_name(raw)
    skill_dir = _resolve_skill_dir(normalized)
    if skill_dir is None:
        return None
    skill_md = skill_dir / "SKILL.md"
    try:
        raw_content = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    frontmatter, body = discovery.parse_frontmatter(raw_content)
    name = str(frontmatter.get("name") or skill_dir.name)
    loaded = {
        "success": True,
        "name": name,
        "content": body,
        "raw_content": raw_content,
        "path": str(skill_dir),
        "skill_dir": str(skill_dir),
        "frontmatter": frontmatter,
    }
    return loaded, skill_dir, name


def _supporting_files(skill_dir: Optional[Path]) -> List[str]:
    """列出技能支持区（references/templates/scripts/assets/examples）下的相对文件路径。"""
    supporting: List[str] = []
    if not skill_dir:
        return supporting
    for subdir in sorted(SKILL_SUPPORT_DIRS):
        subdir_path = skill_dir / subdir
        if not subdir_path.exists():
            continue
        for f in sorted(subdir_path.rglob("*")):
            if f.is_file() and not f.is_symlink():
                supporting.append(str(f.relative_to(skill_dir)).replace("\\", "/"))
    return supporting


def _build_skill_message(
    loaded_skill: Dict[str, Any],
    skill_dir: Optional[Path],
    activation_note: str,
    user_instruction: str = "",
    runtime_note: str = "",
    session_id: Optional[str] = None,
) -> str:
    """把载入的技能格式化为注入对话的用户消息载荷（对标 Hermes ``_build_skill_message``）。"""
    content = str(loaded_skill.get("content") or "")

    # 模板变量替换 + 内联 shell 展开（先做，故下游块看到展开后的正文）。
    content = preprocessing.preprocess_skill_content(content, skill_dir, session_id=session_id)

    parts = [activation_note, "", content.strip()]

    # 注入技能绝对目录，便于 agent 直接引用捆绑脚本而无需再 skill_view 一趟。
    if skill_dir:
        parts.append("")
        parts.append(f"[Skill directory: {skill_dir}]")
        parts.append(
            "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
            "`templates/config.yaml`) against that directory, then run them with "
            "the terminal tool using the absolute path."
        )

    supporting = _supporting_files(skill_dir)
    if supporting and skill_dir:
        parts.append("")
        parts.append("[This skill has supporting files:]")
        for sf in supporting:
            parts.append(f"- {sf}  ->  {skill_dir / sf}")
        parts.append(
            f'\nLoad any of these with skill_view(name="{skill_dir.name}", '
            f'file="<path>"), or run scripts directly by absolute path.'
        )

    if user_instruction:
        parts.append("")
        parts.append(
            "The user has provided the following instruction alongside the skill "
            f"invocation: {user_instruction}"
        )

    if runtime_note:
        parts.append("")
        parts.append(f"[Runtime note: {runtime_note}]")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 扫描 / 缓存 / 重载
# ---------------------------------------------------------------------------

def scan_skill_commands() -> Dict[str, Dict[str, Any]]:
    """扫描所有技能目录，返回 ``{"/slug": {name, description, skill_md_path, skill_dir}}``。

    施加平台 / 环境 / 禁用门；slug 冲突时首个胜出（本地先于外部）；与核心 slash 命令
    冲突的技能跳过自动注册（仍可经 ``/skill <name>`` 载入）。
    """
    global _skill_commands, _skill_commands_platform
    _skill_commands_platform = _resolve_skill_commands_platform()
    _skill_commands = {}

    from spirit.skills_hub.dispatch import RESERVED_SKILL_COMMANDS

    disabled = discovery.get_disabled_skill_names()
    seen_names: set = set()

    try:
        for scan_dir in discovery.get_all_skills_dirs():
            if not scan_dir.is_dir():
                continue
            for skill_md in discovery.iter_skill_index_files(scan_dir, "SKILL.md"):
                try:
                    content = skill_md.read_text(encoding="utf-8", errors="replace")
                    frontmatter, body = discovery.parse_frontmatter(content)
                    if not discovery.skill_matches_platform(frontmatter):
                        continue
                    if not discovery.skill_matches_environment(frontmatter):
                        continue
                    name = str(frontmatter.get("name") or skill_md.parent.name)
                    if name in seen_names or name in disabled:
                        continue

                    description = str(frontmatter.get("description") or "")
                    if not description:
                        for line in body.strip().split("\n"):
                            line = line.strip()
                            if line and not line.startswith("#"):
                                description = line[:80]
                                break

                    cmd_name = slugify(name)
                    if not cmd_name:
                        continue
                    # 与保留的核心 slash 命令冲突 → 跳过自动注册（仍可 /skill <name>）。
                    if cmd_name in RESERVED_SKILL_COMMANDS:
                        logger.warning(
                            "技能 %r 生成的 slash 命令 '/%s' 与核心命令冲突；跳过自动注册，"
                            "改用 '/skill %s'。", name, cmd_name, name,
                        )
                        continue
                    seen_names.add(name)
                    cmd_key = f"/{cmd_name}"
                    if cmd_key in _skill_commands:
                        logger.warning(
                            "技能 %r 映射到已被 %r 占用的 %s；保留首个，跳过此项。",
                            name, _skill_commands[cmd_key]["name"], cmd_key,
                        )
                        continue
                    _skill_commands[cmd_key] = {
                        "name": name,
                        "description": description or f"Invoke the {name} skill",
                        "skill_md_path": str(skill_md),
                        "skill_dir": str(skill_md.parent),
                    }
                except Exception:
                    continue
    except Exception:
        logger.debug("技能命令扫描失败", exc_info=True)

    return _skill_commands


def get_skill_commands() -> Dict[str, Dict[str, Any]]:
    """返回当前技能命令映射（空或平台作用域变化时重扫）。"""
    if not _skill_commands or _skill_commands_platform != _resolve_skill_commands_platform():
        scan_skill_commands()
    return _skill_commands


def invalidate_skill_commands() -> None:
    """测试钩子 / 配置变更后：清空扫描缓存，强制下次重扫。"""
    global _skill_commands, _skill_commands_platform
    _skill_commands = {}
    _skill_commands_platform = None


def reload_skills() -> Dict[str, Any]:
    """重扫技能目录并返回差异（``added`` / ``removed`` / ``unchanged`` / ``total`` / ``commands``）。

    对标 Hermes ``reload_skills``：不失效系统提示缓存（技能按名调用，无需进系统提示），
    故 ``/reload-skills`` 不付缓存重置成本。
    """
    def _snapshot(cmds: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        return {k.lstrip("/"): (v or {}).get("description") or "" for k, v in cmds.items()}

    before = _snapshot(_skill_commands)
    new_commands = scan_skill_commands()
    after = _snapshot(new_commands)

    added_names = sorted(set(after) - set(before))
    removed_names = sorted(set(before) - set(after))
    unchanged = sorted(set(after) & set(before))

    return {
        "added": [{"name": n, "description": after[n]} for n in added_names],
        "removed": [{"name": n, "description": before[n]} for n in removed_names],
        "unchanged": unchanged,
        "total": len(after),
        "commands": len(new_commands),
    }


def resolve_skill_command_key(command: str) -> Optional[str]:
    """把用户输入的 ``/command`` 解析为规范 ``/slug``（连字符 / 下划线互通）。"""
    if not command:
        return None
    cmd_key = f"/{command.replace('_', '-')}"
    return cmd_key if cmd_key in get_skill_commands() else None


# ---------------------------------------------------------------------------
# 调用消息构建
# ---------------------------------------------------------------------------

def build_skill_invocation_message(
    cmd_key: str,
    user_instruction: str = "",
    task_id: Optional[str] = None,
    runtime_note: str = "",
) -> Optional[str]:
    """构建单个技能 slash 调用的用户消息（找不到技能返回 None）。"""
    commands = get_skill_commands()
    skill_info = commands.get(cmd_key)
    if not skill_info:
        return None

    loaded = _load_skill_payload(skill_info["skill_dir"], task_id=task_id)
    if not loaded:
        return None
    loaded_skill, skill_dir, skill_name = loaded

    try:
        usage.bump_use(skill_name)
    except Exception:
        pass

    activation_note = (
        f'[IMPORTANT: The user has invoked the "{skill_name}" skill, indicating they want '
        "you to follow its instructions. The full skill content is loaded below.]"
    )
    return _build_skill_message(
        loaded_skill, skill_dir, activation_note,
        user_instruction=user_instruction, runtime_note=runtime_note, session_id=task_id,
    )


def split_stacked_skill_commands(rest: str) -> Tuple[List[str], str]:
    """从 *rest* 消费额外的前导 ``/skill`` token（至多 ``_MAX_STACKED_SKILLS - 1`` 个）。

    *rest* 是首个已匹配技能命令之后的文本。前导的、以 ``/`` 开头且能解析为已安装技能命令的
    token 被消费；遇到首个不可解析的 token 即停，该 token 及其后成为用户指令。返回
    ``(extra_cmd_keys, remaining_instruction)``。
    """
    keys: List[str] = []
    remaining = rest or ""
    while len(keys) < _MAX_STACKED_SKILLS - 1:
        stripped = remaining.lstrip()
        if not stripped.startswith("/"):
            break
        parts = stripped.split(None, 1)
        token = parts[0]
        tail = parts[1] if len(parts) > 1 else ""
        cmd_key = resolve_skill_command_key(token.lstrip("/"))
        if cmd_key is None or cmd_key in keys:
            break
        keys.append(cmd_key)
        remaining = tail
    return keys, remaining.strip()


def build_stacked_skill_invocation_message(
    cmd_keys: List[str],
    user_instruction: str = "",
    task_id: Optional[str] = None,
) -> Optional[Tuple[str, List[str], List[str]]]:
    """构建堆叠多技能 slash 调用的用户消息。

    返回 ``(message, loaded_skill_names, missing_skill_names)``；一个技能都载不入时 None。
    消息头刻意含 " skill bundle," 且每块以 ``[Loaded as part of the `` 前缀开头，复用
    bundle 脚手架标记，便于 :func:`extract_user_instruction_from_skill_message` 回收指令。
    """
    commands = get_skill_commands()
    loaded_names: List[str] = []
    missing: List[str] = []
    skill_blocks: List[str] = []
    seen: set = set()

    for cmd_key in cmd_keys:
        if not cmd_key or cmd_key in seen:
            continue
        seen.add(cmd_key)
        skill_info = commands.get(cmd_key)
        if not skill_info:
            missing.append(cmd_key.lstrip("/"))
            continue
        loaded = _load_skill_payload(skill_info["skill_dir"], task_id=task_id)
        if not loaded:
            missing.append(cmd_key.lstrip("/"))
            continue
        loaded_skill, skill_dir, skill_name = loaded

        try:
            usage.bump_use(skill_name)
        except Exception:
            pass

        activation_note = f'[Loaded as part of the stacked skill invocation "{skill_name}".]'
        skill_blocks.append(_build_skill_message(loaded_skill, skill_dir, activation_note, session_id=task_id))
        loaded_names.append(skill_name)

    if not skill_blocks:
        return None

    typed = " ".join(k for k in cmd_keys if k)
    header_lines = [
        f'[IMPORTANT: The user has invoked the "{typed}" stacked skill bundle, '
        f"loading {len(loaded_names)} skills together. Treat every skill below "
        "as active guidance for this turn.]",
        "",
        f"Skills loaded: {', '.join(loaded_names)}",
    ]
    if missing:
        header_lines.append(f"Skills missing (skipped): {', '.join(missing)}")
    if user_instruction:
        header_lines.extend(["", f"User instruction: {user_instruction}"])

    header = "\n".join(header_lines)
    return ("\n\n".join([header, *skill_blocks]), loaded_names, missing)


def build_preloaded_skills_prompt(
    skill_identifiers: List[str],
    task_id: Optional[str] = None,
) -> Tuple[str, List[str], List[str]]:
    """为会话级预加载载入一或多个技能，返回 ``(prompt_text, loaded_names, missing)``。

    禁用技能视同缺失：经原始标识符直接进 ``_load_skill_payload``，绕过 ``get_skill_commands()``
    的扫描期禁用过滤，故须在此重新施加禁用门（对标 Hermes bundle 门）。
    """
    prompt_parts: List[str] = []
    loaded_names: List[str] = []
    missing: List[str] = []

    try:
        disabled_names = discovery.get_disabled_skill_names()
    except Exception:
        disabled_names = set()

    seen: set = set()
    for raw_identifier in skill_identifiers:
        identifier = (raw_identifier or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        loaded = _load_skill_payload(identifier, task_id=task_id)
        if not loaded:
            missing.append(identifier)
            continue
        loaded_skill, skill_dir, skill_name = loaded

        if skill_name in disabled_names or identifier in disabled_names:
            missing.append(identifier)
            continue

        try:
            usage.bump_use(skill_name)
        except Exception:
            pass

        activation_note = (
            f'[IMPORTANT: The user launched this session with the "{skill_name}" skill '
            "preloaded. Treat its instructions as active guidance for the duration of this "
            "session unless the user overrides them.]"
        )
        prompt_parts.append(_build_skill_message(loaded_skill, skill_dir, activation_note, session_id=task_id))
        loaded_names.append(skill_name)

    return "\n\n".join(prompt_parts), loaded_names, missing


# ---------------------------------------------------------------------------
# 指令回收（供记忆脚手架：存用户真正问的，而非 N 段技能正文）
# ---------------------------------------------------------------------------

_SINGLE_MARKER = (
    "The user has provided the following instruction alongside the skill invocation: "
)


def _extract_single_skill_user_instruction(message: str) -> Optional[str]:
    idx = message.find(_SINGLE_MARKER)
    if idx == -1:
        return None
    tail = message[idx + len(_SINGLE_MARKER):]
    return tail.strip() or None


def _extract_bundle_user_instruction(message: str) -> Optional[str]:
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith("User instruction:"):
            value = stripped[len("User instruction:"):].strip()
            return value or None
    return None


def extract_user_instruction_from_skill_message(content: Any) -> Optional[str]:
    """从技能 / 捆绑调用消息里回收用户指令（无则 None）。

    ``content`` 可为字符串或消息 dict 列表（取首条 user 消息的文本）。bundle 格式
    （含 " skill bundle,"）走 ``User instruction:`` 行；单技能格式走 invocation 标记。
    """
    message: Optional[str] = None
    if isinstance(content, str):
        message = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("role") == "user":
                text = item.get("content")
                if isinstance(text, str):
                    message = text
                    break
    if not message:
        return None
    if " skill bundle," in message or "stacked skill bundle," in message:
        return _extract_bundle_user_instruction(message)
    return _extract_single_skill_user_instruction(message)


__all__ = [
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
]
