"""项目管理工具 — git_status / git_diff / project_info。

提供 Git 操作和项目结构分析能力。
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


def _run_git(args: list, cwd: str = None) -> tuple:
    """运行 git 命令，返回 (success, output)。"""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except FileNotFoundError:
        return False, "git 未安装或不在 PATH 中"
    except subprocess.TimeoutExpired:
        return False, "git 命令超时"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------

GIT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": "获取 Git 仓库状态（修改/暂存/未跟踪的文件）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓库路径（默认当前目录）"},
            },
            "required": [],
        },
    },
}


def _git_status_impl(path: str = ".") -> str:
    cwd = str(Path(path).resolve())

    ok, branch = _run_git(["branch", "--show-current"], cwd)
    if not ok:
        return json.dumps({"error": "不是 Git 仓库或 git 不可用"})

    ok, status_output = _run_git(["status", "--porcelain=v1"], cwd)
    if not ok:
        return json.dumps({"error": status_output})

    # 解析 status 输出
    staged, unstaged, untracked = [], [], []
    for line in status_output.splitlines():
        if len(line) < 3:
            continue
        x, y = line[0], line[1]
        filepath = line[3:]

        if x == "?":
            untracked.append(filepath)
        elif x != " ":
            staged.append({"file": filepath, "status": x})
        if y != " " and x != "?":
            unstaged.append({"file": filepath, "status": y})

    # 统计
    ok, ahead_behind = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cwd)
    ahead_behind_info = ahead_behind if ok else None

    return json.dumps({
        "branch": branch,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "summary": {
            "staged": len(staged),
            "unstaged": len(unstaged),
            "untracked": len(untracked),
        },
    }, ensure_ascii=False, indent=2)


registry.register(
    name="git_status",
    toolset="project",
    schema=GIT_STATUS_SCHEMA,
    handler=_git_status_impl,
    description="Git 仓库状态",
    emoji="🔀",
)


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------

GIT_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": "查看 Git diff（工作区/暂存区/指定提交之间的差异）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓库路径"},
                "staged": {"type": "boolean", "description": "是否查看暂存区（默认 false）"},
                "files": {"type": "string", "description": "指定文件路径（可选）"},
                "context_lines": {"type": "integer", "description": "上下文行数（默认 3）"},
            },
            "required": [],
        },
    },
}


def _git_diff_impl(
    path: str = ".",
    staged: bool = False,
    files: str = None,
    context_lines: int = 3,
) -> str:
    cwd = str(Path(path).resolve())
    args = ["diff"]
    if staged:
        args.append("--cached")
    args.extend(["-U", str(context_lines)])
    if files:
        args.extend(["--"] + files.split())

    ok, output = _run_git(args, cwd)
    if not ok:
        return json.dumps({"error": output})

    # 截断大 diff
    from spirit.config import get_config_value
    max_chars = get_config_value("limits.diff_max_chars", 20000)
    if len(output) > max_chars:
        output = output[:max_chars] + f"\n\n... [diff 截断，共 {len(output)} 字符]"

    return json.dumps({
        "diff": output,
        "staged": staged,
        "length": len(output),
    })


registry.register(
    name="git_diff",
    toolset="project",
    schema=GIT_DIFF_SCHEMA,
    handler=_git_diff_impl,
    description="查看 Git diff",
    emoji="📝",
)


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------

GIT_LOG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_log",
        "description": "查看 Git 提交历史。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓库路径"},
                "count": {"type": "integer", "description": "显示条数（默认 10）"},
                "oneline": {"type": "boolean", "description": "单行格式（默认 true）"},
            },
            "required": [],
        },
    },
}


def _git_log_impl(path: str = ".", count: int = 10, oneline: bool = True) -> str:
    cwd = str(Path(path).resolve())
    count = min(max(count, 1), 50)

    args = ["log", f"-{count}"]
    if oneline:
        args.append("--oneline")
    else:
        args.extend(["--format=%h %s (%an, %ar)"])

    ok, output = _run_git(args, cwd)
    if not ok:
        return json.dumps({"error": output})

    return json.dumps({"log": output, "count": count})


registry.register(
    name="git_log",
    toolset="project",
    schema=GIT_LOG_SCHEMA,
    handler=_git_log_impl,
    description="Git 提交历史",
    emoji="📜",
)


# ---------------------------------------------------------------------------
# project_info
# ---------------------------------------------------------------------------

PROJECT_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "project_info",
        "description": (
            "分析项目结构：检测语言/框架、统计文件数量、识别项目类型。\n"
            "帮助理解一个陌生项目的全貌。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目根目录（默认当前目录）"},
            },
            "required": [],
        },
    },
}

# 语言 → 文件扩展名映射
LANGUAGE_EXTENSIONS = {
    "Python": {".py"},
    "JavaScript": {".js", ".mjs", ".cjs"},
    "TypeScript": {".ts", ".tsx"},
    "Java": {".java"},
    "Go": {".go"},
    "Rust": {".rs"},
    "C/C++": {".c", ".cpp", ".cc", ".h", ".hpp"},
    "Ruby": {".rb"},
    "PHP": {".php"},
    "Swift": {".swift"},
    "Kotlin": {".kt", ".kts"},
    "Shell": {".sh", ".bash", ".zsh"},
    "HTML": {".html", ".htm"},
    "CSS": {".css", ".scss", ".sass", ".less"},
    "YAML": {".yaml", ".yml"},
    "JSON": {".json"},
    "Markdown": {".md"},
}

# 框架检测标志
FRAMEWORK_MARKERS = {
    "React": ["react", "react-dom"],
    "Vue": ["vue"],
    "Next.js": ["next"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Express": ["express"],
    "Spring": ["spring-boot"],
    "Rails": ["rails"],
}


def _project_info_impl(path: str = ".") -> str:
    target = Path(path).resolve()
    if not target.is_dir():
        return json.dumps({"error": f"不是目录: {path}"})

    # 统计文件
    ext_count = {}
    total_files = 0
    total_size = 0
    ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}

    for root, dirs, files in os.walk(target):
        # 过滤忽略目录
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        for fname in files:
            fpath = Path(root) / fname
            ext = fpath.suffix.lower()
            if ext:
                ext_count[ext] = ext_count.get(ext, 0) + 1
            total_files += 1
            try:
                total_size += fpath.stat().st_size
            except OSError:
                pass

    # 识别主要语言
    languages = {}
    for ext, count in sorted(ext_count.items(), key=lambda x: -x[1]):
        for lang, exts in LANGUAGE_EXTENSIONS.items():
            if ext in exts:
                languages[lang] = languages.get(lang, 0) + count
                break

    top_languages = sorted(languages.items(), key=lambda x: -x[1])[:5]

    # 检测框架
    frameworks = []
    pkg_files = ["package.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod", "Gemfile"]
    dep_content = ""
    for pkg_file in pkg_files:
        pkg_path = target / pkg_file
        if pkg_path.exists():
            try:
                dep_content += pkg_path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pass

    for framework, markers in FRAMEWORK_MARKERS.items():
        if any(marker in dep_content for marker in markers):
            frameworks.append(framework)

    # 检测项目类型
    project_type = "unknown"
    if (target / "package.json").exists():
        project_type = "nodejs"
    elif (target / "pyproject.toml").exists() or (target / "setup.py").exists():
        project_type = "python"
    elif (target / "Cargo.toml").exists():
        project_type = "rust"
    elif (target / "go.mod").exists():
        project_type = "go"
    elif (target / "pom.xml").exists() or (target / "build.gradle").exists():
        project_type = "java"

    # README
    readme = None
    for name in ["README.md", "README.rst", "README.txt", "README"]:
        if (target / name).exists():
            readme = name
            break

    return json.dumps({
        "path": str(target),
        "name": target.name,
        "type": project_type,
        "total_files": total_files,
        "total_size": f"{total_size / 1024 / 1024:.1f}MB",
        "languages": dict(top_languages),
        "frameworks": frameworks,
        "readme": readme,
        "has_git": (target / ".git").exists(),
        "top_extensions": dict(sorted(ext_count.items(), key=lambda x: -x[1])[:10]),
    }, ensure_ascii=False, indent=2)


registry.register(
    name="project_info",
    toolset="project",
    schema=PROJECT_INFO_SCHEMA,
    handler=_project_info_impl,
    description="分析项目结构",
    emoji="🏗️",
)
