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
# 技能目录与配置
# ============================================================================

from spirit.config import SPIRIT_HOME, get_config_value

SKILLS_DIR = SPIRIT_HOME / "skills"
HUB_DIR = SKILLS_DIR / ".hub"
LOCK_FILE = HUB_DIR / "lock.json"
QUARANTINE_DIR = HUB_DIR / "quarantine"
AUDIT_LOG = HUB_DIR / "audit.jsonl"
INDEX_CACHE_TTL = get_config_value("internal.skills_index_cache_ttl", 3600)

EXCLUDED_SKILL_DIRS = {".hub", ".git", "__pycache__", "node_modules", ".DS_Store"}


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
# 技能发现与解析
# ============================================================================

def _split_frontmatter(text: str) -> Optional[Dict[str, Any]]:
    """解析 YAML frontmatter。"""
    if not isinstance(text, str):
        return None
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped.startswith("---"):
        return None
    after_open = stripped[3:]
    end = after_open.find("\n---")
    if end == -1:
        return None
    fm_text = after_open[:end]
    try:
        import yaml
        data = yaml.safe_load(fm_text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@dataclass
class SkillMeta:
    name: str
    description: str
    version: str = ""
    path: str = ""
    tags: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)


def _parse_skill_md(skill_md_path: Path) -> Optional[SkillMeta]:
    """解析 SKILL.md 文件的元数据。"""
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm = _split_frontmatter(text)
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
    hermes_meta = meta.get("hermes", {}) if isinstance(meta, dict) else {}
    tags = hermes_meta.get("tags", []) if isinstance(hermes_meta, dict) else []

    return SkillMeta(
        name=name, description=desc,
        version=str(fm.get("version", "")),
        path=str(skill_md_path.parent),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        platforms=[str(p) for p in fm.get("platforms", [])] if isinstance(fm.get("platforms"), list) else [],
    )


def _find_all_skills() -> List[SkillMeta]:
    """扫描技能目录，返回所有技能元数据。"""
    skills = []
    base = SKILLS_DIR
    if not base.is_dir():
        return skills

    for skill_md in base.rglob("SKILL.md"):
        if any(part in EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        meta = _parse_skill_md(skill_md)
        if meta:
            skills.append(meta)

    return sorted(skills, key=lambda s: s.name.lower())


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

    # 查找技能目录
    candidates = list(SKILLS_DIR.rglob(f"**/{name}/SKILL.md"))
    if not candidates:
        candidates = list(SKILLS_DIR.glob(f"*/{name}/SKILL.md")) + \
                     list(SKILLS_DIR.glob(f"{name}/SKILL.md"))

    if not candidates:
        return json.dumps({"error": f"技能 '{name}' 未找到"})

    skill_dir = candidates[0].parent

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
        content = candidates[0].read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return json.dumps({"error": str(exc)})

    # 列出引用文件
    ref_files = []
    for f in skill_dir.rglob("*"):
        if f.is_file() and f.name != "SKILL.md":
            ref_files.append(str(f.relative_to(skill_dir)))

    return json.dumps({
        "name": name, "content": content,
        "files": ref_files[:50],
    }, ensure_ascii=False)


registry.register(name="skills_list", toolset="skills", schema=SKILLS_LIST_SCHEMA,
                  handler=_handle_skills_list, check_fn=lambda: SKILLS_DIR.is_dir(), emoji="📚")
registry.register(name="skill_view", toolset="skills", schema=SKILL_VIEW_SCHEMA,
                  handler=_handle_skill_view, check_fn=lambda: SKILLS_DIR.is_dir(), emoji="📖")


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
        candidates = list(SKILLS_DIR.rglob(f"**/{name}"))
        if not candidates:
            return json.dumps({"error": f"技能 '{name}' 未找到"})
        dirs_to_scan = [d for d in candidates if d.is_dir()]
    else:
        dirs_to_scan = [d for d in SKILLS_DIR.rglob("*/SKILL.md")
                        if not any(p in EXCLUDED_SKILL_DIRS for p in d.parts)]
        dirs_to_scan = [d.parent for d in dirs_to_scan]

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
                  handler=_handle_skills_scan, check_fn=lambda: SKILLS_DIR.is_dir(), emoji="🛡️")
