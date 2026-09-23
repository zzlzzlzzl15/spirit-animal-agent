"""技能系统工具 — Spirit Agent。

合并自 Hermes:
- skills_tool.py: 技能列表/查看（渐进式披露）
- skills_hub.py: 技能市场（GitHub 源适配、锁文件、索引缓存）
- skills_sync.py: 技能同步
- skills_guard.py: 安全扫描（恶意模式检测）
- skill_usage.py: 技能使用追踪
- skill_provenance.py: 技能来源追踪
- skill_manager_tool.py: 技能管理工具
- skills_ast_audit.py: AST 审计
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# 技能目录与配置（委托 skills_hub.paths — 按调用解析，尊重 SPIRIT_HOME / env 覆盖）
# ============================================================================

from spirit.config import get_config_value
from spirit.skills_hub import discovery as _discovery
from spirit.skills_hub import paths as _paths

# 向后兼容的模块级常量（导入期快照）。新代码应调用 ``_paths.skills_dir()`` 等函数，
# 以便测试 monkeypatch ``config.SPIRIT_HOME`` 或 env 覆盖能即时生效。
SKILLS_DIR = _paths.skills_dir()
HUB_DIR = _paths.hub_dir()
LOCK_FILE = _paths.lock_file()
QUARANTINE_DIR = _paths.quarantine_dir()
AUDIT_LOG = _paths.audit_log()
INDEX_CACHE_TTL = get_config_value("internal.skills_index_cache_ttl", 3600)

EXCLUDED_SKILL_DIRS = set(_paths.EXCLUDED_SKILL_DIRS)


# ============================================================================
# Skills Guard — 安全扫描
# ============================================================================

SCANNER_VERSION = "skills-guard-v1"

TRUSTED_REPOS = {
    "openai/skills", "anthropics/skills", "huggingface/skills", "NVIDIA/skills",
}

# 安全扫描模式
DANGEROUS_PATTERNS = [
    (r"os\.system\s*\(", "os_command_exec"),
    (r"subprocess\.(?:call|run|Popen)\s*\(", "subprocess_exec"),
    (r"exec\s*\(", "exec_eval"),
    (r"eval\s*\(", "exec_eval"),
    (r"__import__\s*\(", "dynamic_import"),
    (r"urllib\.request\.urlretrieve\s*\(", "file_download"),
    (r"socket\.connect\s*\(", "raw_network"),
    (r"base64\.b64decode\s*\(", "encoded_payload"),
    (r"requests\.get\s*\(.*\+.*\)", "url_injection"),
    (r"open\s*\(.*['\"]w['\"]", "file_write"),
    (r"shutil\.rmtree\s*\(", "recursive_delete"),
    (r"curl\s+.*\|\s*(?:bash|sh)\s*", "pipe_to_shell"),
    (r"wget\s+.*\|\s*(?:bash|sh)\s*", "pipe_to_shell"),
    (r"ignore\s+(?:\w+\s+)*(?:previous|all)\s+instructions", "prompt_injection"),
    (r"do\s+not\s+tell\s+the\s+user", "deception"),
]


@dataclass
class Finding:
    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str


@dataclass
class ScanResult:
    verdict: str  # "safe" | "caution" | "dangerous"
    findings: List[Finding] = field(default_factory=list)
    files_scanned: int = 0
    source: str = ""


def scan_skill(skill_dir: Path, source: str = "community") -> ScanResult:
    """扫描技能目录中的文件，检测危险模式。"""
    findings: List[Finding] = []
    files_scanned = 0

    for fpath in skill_dir.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix not in {".md", ".py", ".js", ".ts", ".sh", ".yaml", ".yml"}:
            continue
        files_scanned += 1
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern, pattern_id in DANGEROUS_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    severity = "critical" if pattern_id in ("exec_eval", "prompt_injection",
                                                             "deception", "pipe_to_shell") else "high"
                    category = "injection" if "injection" in pattern_id else "exfiltration"
                    findings.append(Finding(
                        pattern_id=pattern_id, severity=severity, category=category,
                        file=str(fpath.relative_to(skill_dir)), line=line_no,
                        match=line.strip()[:200],
                    ))

    has_critical = any(f.severity == "critical" for f in findings)
    has_high = any(f.severity in ("critical", "high") for f in findings)
    if has_critical:
        verdict = "dangerous"
    elif has_high:
        verdict = "caution"
    else:
        verdict = "safe"

    return ScanResult(verdict=verdict, findings=findings,
                      files_scanned=files_scanned, source=source)


def format_scan_report(result: ScanResult) -> str:
    lines = [f"扫描结果: {result.verdict.upper()}", f"扫描文件: {result.files_scanned}"]
    for f in result.findings:
        lines.append(f"  [{f.severity}] {f.pattern_id} in {f.file}:{f.line}")
        lines.append(f"    {f.match}")
    return "\n".join(lines)


# ============================================================================
# 技能发现与解析（委托 skills_hub.discovery — 单一事实源）
# ============================================================================

# 向后兼容别名：discovery.SkillMeta 是本包唯一的技能元数据模型（含 environments）。
SkillMeta = _discovery.SkillMeta


def _split_frontmatter(text: str) -> Optional[Dict[str, Any]]:
    """解析 YAML frontmatter（委托 discovery.parse_frontmatter，返回 dict 或 None）。"""
    fm, _body = _discovery.parse_frontmatter(text if isinstance(text, str) else "")
    return fm or None


def _parse_skill_md(skill_md_path: Path) -> Optional[SkillMeta]:
    """解析 SKILL.md 文件的元数据（委托 discovery.parse_skill_md）。"""
    return _discovery.parse_skill_md(skill_md_path)


def _find_all_skills() -> List[SkillMeta]:
    """扫描所有技能根，返回按名排序的技能元数据（委托 discovery.find_all_skills）。"""
    return _discovery.find_all_skills()


# ============================================================================
# 技能列表/查看工具
# ============================================================================

SKILLS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_list",
        "description": (
            "列出所有已安装的技能（仅元数据：名称和描述）。"
            "使用 skill_view 查看完整内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "按标签筛选"},
                "platform": {"type": "string", "description": "按平台筛选"},
            },
        },
    },
}


def _handle_skills_list(args: Dict[str, Any], **kwargs) -> str:
    tag = args.get("tag", "").strip().lower()
    platform = args.get("platform", "").strip().lower()
    skills = _find_all_skills()

    if tag:
        skills = [s for s in skills if tag in [t.lower() for t in s.tags]]
    if platform:
        current_platform = os.name  # 'posix' or 'nt'
        platform_map = {"macos": "darwin", "linux": "posix", "windows": "nt"}
        target = platform_map.get(platform, platform)
        skills = [s for s in skills if not s.platforms or target in
                  [platform_map.get(p, p) for p in s.platforms]]

    result = [{"name": s.name, "description": s.description, "version": s.version} for s in skills]
    return json.dumps({"skills": result, "count": len(result)}, ensure_ascii=False)


SKILL_VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": "查看技能的完整内容（指令、引用文件等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名称"},
                "file": {"type": "string", "description": "可选的引用文件路径"},
            },
            "required": ["name"],
        },
    },
}


def _handle_skill_view(args: Dict[str, Any], **kwargs) -> str:
    name = args.get("name", "").strip()
    if not name:
        return json.dumps({"error": "name 不能为空"})

    file_ref = args.get("file", "").strip()

    # 查找技能目录（委托 commands._resolve_skill_dir：名 / 相对路径 / frontmatter 名，跨所有技能根）。
    from spirit.skills_hub import commands as _commands

    skill_dir = _commands._resolve_skill_dir(_discovery.normalize_skill_lookup_name(name))
    if skill_dir is None:
        return json.dumps({"error": f"技能 '{name}' 未找到"})

    if file_ref:
        target = skill_dir / file_ref
        if not target.exists() or not target.is_file():
            return json.dumps({"error": f"文件 '{file_ref}' 不存在"})
        # 安全检查：防止路径遍历
        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            return json.dumps({"error": "路径遍历被阻止"})
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            return json.dumps({"name": name, "file": file_ref, "content": content})
        except OSError as exc:
            return json.dumps({"error": str(exc)})

    # 读取主 SKILL.md
    try:
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return json.dumps({"error": str(exc)})

    # 列出引用文件
    ref_files = []
    for f in skill_dir.rglob("*"):
        if f.is_file() and f.name != "SKILL.md":
            ref_files.append(str(f.relative_to(skill_dir)))

    # 记一次查看（供 /skill info 的活跃度统计）。
    try:
        from spirit.skills_hub import usage as _usage

        _usage.bump_view(name)
    except Exception:
        pass

    return json.dumps({
        "name": name, "content": content,
        "files": ref_files[:50],
    }, ensure_ascii=False)


registry.register(name="skills_list", toolset="skills", schema=SKILLS_LIST_SCHEMA,
                  handler=_handle_skills_list, check_fn=lambda: _paths.skills_dir().is_dir(), emoji="📚")
registry.register(name="skill_view", toolset="skills", schema=SKILL_VIEW_SCHEMA,
                  handler=_handle_skill_view, check_fn=lambda: _paths.skills_dir().is_dir(), emoji="📖")


# ============================================================================
# 技能安全扫描工具
# ============================================================================

SKILLS_SCAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_scan",
        "description": "扫描已安装技能的安全问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "指定技能名（可选，默认扫描全部）"},
            },
        },
    },
}


def _handle_skills_scan(args: Dict[str, Any], **kwargs) -> str:
    name = args.get("name", "").strip()
    results = []

    if name:
        from spirit.skills_hub import commands as _commands

        skill_dir = _commands._resolve_skill_dir(_discovery.normalize_skill_lookup_name(name))
        if skill_dir is None:
            return json.dumps({"error": f"技能 '{name}' 未找到"})
        dirs_to_scan = [skill_dir]
    else:
        dirs_to_scan = [Path(m.path) for m in _discovery.find_all_skills() if m.path]

    for skill_dir in dirs_to_scan[:50]:
        result = scan_skill(skill_dir)
        results.append({
            "skill": skill_dir.name,
            "verdict": result.verdict,
            "files_scanned": result.files_scanned,
            "findings_count": len(result.findings),
        })

    return json.dumps({"results": results, "total": len(results)}, ensure_ascii=False)


registry.register(name="skills_scan", toolset="skills", schema=SKILLS_SCAN_SCHEMA,
                  handler=_handle_skills_scan, check_fn=lambda: _paths.skills_dir().is_dir(), emoji="🛡️")


# ============================================================================
# 技能市场工具（委托 skills_hub.hub — 浏览 / 安装 / 卸载）
# ============================================================================

SKILLS_BROWSE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_browse",
        "description": "浏览 / 搜索技能市场索引（按名 / 描述 / 标签子串；空 query 返回全部）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词（可选）"},
                "source": {"type": "string", "description": "源名（默认 community）"},
            },
        },
    },
}


def _handle_skills_browse(args: Dict[str, Any], **kwargs) -> str:
    from spirit.skills_hub import hub as _hub

    query = (args.get("query") or "").strip()
    source = (args.get("source") or "community").strip() or "community"
    try:
        entries = _hub.search_skills(query, source=source) if query else _hub.browse_index(source)
    except _hub.HubError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"entries": entries[:50], "count": len(entries)}, ensure_ascii=False)


SKILLS_INSTALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_install",
        "description": "从市场获取并安装一个技能（隔离 → 安全扫描 → 安装 / 留隔离）。",
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "技能标识符（如 owner/repo 或源内名）"},
                "source": {"type": "string", "description": "源名（默认 community）"},
                "category": {"type": "string", "description": "安装子目录（可选）"},
            },
            "required": ["identifier"],
        },
    },
}


def _handle_skills_install(args: Dict[str, Any], **kwargs) -> str:
    from spirit.skills_hub import hub as _hub
    from spirit.skills_hub import commands as _commands

    identifier = (args.get("identifier") or "").strip()
    if not identifier:
        return json.dumps({"error": "identifier 不能为空"}, ensure_ascii=False)
    source = (args.get("source") or "community").strip() or "community"
    category = (args.get("category") or "").strip()
    ok, message = _hub.install_skill(identifier, source=source, category=category)
    if ok:
        _commands.invalidate_skill_commands()
    return json.dumps({"ok": ok, "message": message}, ensure_ascii=False)


SKILLS_UNINSTALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_uninstall",
        "description": "卸载一个市场安装的技能（拒绝移除非市场安装的 / 内置技能）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名"},
            },
            "required": ["name"],
        },
    },
}


def _handle_skills_uninstall(args: Dict[str, Any], **kwargs) -> str:
    from spirit.skills_hub import hub as _hub
    from spirit.skills_hub import commands as _commands

    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name 不能为空"}, ensure_ascii=False)
    ok, message = _hub.uninstall_skill(name)
    if ok:
        _commands.invalidate_skill_commands()
    return json.dumps({"ok": ok, "message": message}, ensure_ascii=False)


registry.register(name="skills_browse", toolset="skills", schema=SKILLS_BROWSE_SCHEMA,
                  handler=_handle_skills_browse, check_fn=lambda: True, emoji="🌐")
registry.register(name="skills_install", toolset="skills", schema=SKILLS_INSTALL_SCHEMA,
                  handler=_handle_skills_install, check_fn=lambda: True, emoji="📦")
registry.register(name="skills_uninstall", toolset="skills", schema=SKILLS_UNINSTALL_SCHEMA,
                  handler=_handle_skills_uninstall, check_fn=lambda: True, emoji="🗑️")
