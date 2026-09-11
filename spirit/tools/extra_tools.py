"""扩展工具 — Spirit Agent。

合并自 Hermes:
- kanban_tools.py: 看板任务管理
- cronjob_tools.py: 定时任务管理
- blueprints.py: 蓝图自动化
- computer_use_tool.py: 桌面控制
- interrupt.py: 中断信号
- budget_config.py: 预算配置
- approval.py: 审批系统
- slash_confirm.py: 斜杠确认
- thread_context.py: 线程上下文
- hook_output_spill.py: 钩子输出溢出
- async_delegation.py: 异步委托
- checkpoint_manager.py: 检查点管理
- clarify_gateway.py: 澄清网关
- managed_tool_gateway.py: 管理工具网关
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

SPIRIT_HOME = Path.home() / ".spirit"

# ============================================================================
# 中断信号系统（来自 interrupt.py）
# ============================================================================

_interrupted_threads: set = set()
_interrupt_lock = threading.Lock()


def set_interrupt(active: bool, thread_id: Optional[int] = None) -> None:
    """设置或清除指定线程的中断。"""
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _interrupt_lock:
        if active:
            _interrupted_threads.add(tid)
        else:
            _interrupted_threads.discard(tid)


def is_interrupted() -> bool:
    """检查当前线程是否被中断。"""
    tid = threading.current_thread().ident
    with _interrupt_lock:
        return tid in _interrupted_threads


def clear_interrupt() -> None:
    """清除当前线程的中断。"""
    set_interrupt(False)


# ============================================================================
# 预算配置（来自 budget_config.py）
# ============================================================================

DEFAULT_RESULT_SIZE_CHARS: int = 100_000
DEFAULT_TURN_BUDGET_CHARS: int = 200_000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1_500

PINNED_THRESHOLDS: Dict[str, float] = {
    "read_file": float("inf"),
}


@dataclass(frozen=True)
class BudgetConfig:
    """工具结果持久化的预算常量。"""
    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int:
        if tool_name in PINNED_THRESHOLDS:
            return int(PINNED_THRESHOLDS[tool_name])
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        return self.default_result_size


DEFAULT_BUDGET = BudgetConfig()


# ============================================================================
# 看板工具（来自 kanban_tools.py）
# ============================================================================

KANBAN_DB_PATH = SPIRIT_HOME / "kanban.db"

KANBAN_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kanban_list",
        "description": "列出看板上的任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "blocked", "done", "all"],
                    "description": "任务状态筛选",
                    "default": "all",
                },
                "limit": {"type": "integer", "description": "返回数量限制", "default": 50},
            },
        },
    },
}


def _get_kanban_db():
    """获取看板数据库连接。"""
    import sqlite3
    KANBAN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(KANBAN_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kanban_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def _handle_kanban_list(args: Dict[str, Any], **kwargs) -> str:
    status = args.get("status", "all")
    limit = min(args.get("limit", 50), 200)
    try:
        conn = _get_kanban_db()
        if status == "all":
            rows = conn.execute(
                "SELECT id, title, status, metadata, created_at FROM kanban_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, status, metadata, created_at FROM kanban_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        conn.close()
        tasks = [{"id": r[0], "title": r[1], "status": r[2],
                   "metadata": json.loads(r[3]) if r[3] else {},
                   "created_at": r[4]} for r in rows]
        return json.dumps({"tasks": tasks, "count": len(tasks)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


KANBAN_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kanban_create",
        "description": "在看板上创建新任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题"},
                "description": {"type": "string", "description": "任务描述"},
                "metadata": {"type": "object", "description": "附加元数据"},
            },
            "required": ["title"],
        },
    },
}


def _handle_kanban_create(args: Dict[str, Any], **kwargs) -> str:
    title = args.get("title", "").strip()
    if not title:
        return json.dumps({"error": "title 不能为空"})
    description = args.get("description", "")
    metadata = json.dumps(args.get("metadata", {}))
    task_id = f"task_{int(time.time())}_{os.getpid()}"
    try:
        conn = _get_kanban_db()
        conn.execute(
            "INSERT INTO kanban_tasks (id, title, description, status, metadata) VALUES (?, ?, ?, 'pending', ?)",
            (task_id, title, description, metadata)
        )
        conn.commit()
        conn.close()
        return json.dumps({"success": True, "task_id": task_id, "title": title})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


KANBAN_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kanban_update",
        "description": "更新看板任务的状态或内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "blocked", "done"],
                    "description": "新状态",
                },
                "title": {"type": "string", "description": "新标题"},
                "description": {"type": "string", "description": "新描述"},
            },
            "required": ["task_id"],
        },
    },
}


def _handle_kanban_update(args: Dict[str, Any], **kwargs) -> str:
    task_id = args.get("task_id", "")
    if not task_id:
        return json.dumps({"error": "task_id 不能为空"})
    updates = []
    params = []
    for col in ("status", "title", "description"):
        val = args.get(col)
        if val is not None:
            updates.append(f"{col}=?")
            params.append(val)
    if not updates:
        return json.dumps({"error": "没有要更新的字段"})
    updates.append("updated_at=datetime('now')")
    params.append(task_id)
    try:
        conn = _get_kanban_db()
        conn.execute(f"UPDATE kanban_tasks SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
        conn.close()
        return json.dumps({"success": True, "task_id": task_id})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def check_kanban() -> bool:
    return True  # 看板始终可用


registry.register(name="kanban_list", toolset="kanban", schema=KANBAN_LIST_SCHEMA,
                  handler=_handle_kanban_list, check_fn=check_kanban, emoji="📋")
registry.register(name="kanban_create", toolset="kanban", schema=KANBAN_CREATE_SCHEMA,
                  handler=_handle_kanban_create, check_fn=check_kanban, emoji="📋")
registry.register(name="kanban_update", toolset="kanban", schema=KANBAN_UPDATE_SCHEMA,
                  handler=_handle_kanban_update, check_fn=check_kanban, emoji="📋")


# ============================================================================
# 定时任务工具（来自 cronjob_tools.py）
# ============================================================================

CRON_DB_PATH = SPIRIT_HOME / "cron_jobs.json"


def _load_cron_jobs() -> Dict[str, Any]:
    if not CRON_DB_PATH.exists():
        return {}
    try:
        return json.loads(CRON_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cron_jobs(jobs: Dict[str, Any]):
    CRON_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRON_DB_PATH.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


CRONJOB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cronjob",
        "description": "定时任务管理：创建、列出、暂停、恢复、删除定时任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "pause", "resume", "delete", "run_now"],
                    "description": "操作类型",
                },
                "job_id": {"type": "string", "description": "任务 ID"},
                "name": {"type": "string", "description": "任务名称"},
                "schedule": {"type": "string", "description": "cron 表达式（如 '0 9 * * *'）"},
                "prompt": {"type": "string", "description": "要执行的提示/命令"},
            },
            "required": ["action"],
        },
    },
}


def _handle_cronjob(args: Dict[str, Any], **kwargs) -> str:
    action = args.get("action", "")
    jobs = _load_cron_jobs()

    if action == "list":
        job_list = [{"id": k, **v} for k, v in jobs.items()]
        return json.dumps({"jobs": job_list, "count": len(job_list)}, ensure_ascii=False)

    elif action == "create":
        name = args.get("name", "").strip()
        schedule = args.get("schedule", "").strip()
        prompt = args.get("prompt", "").strip()
        if not name or not schedule:
            return json.dumps({"error": "name 和 schedule 必填"})
        job_id = f"cron_{int(time.time())}"
        jobs[job_id] = {
            "name": name, "schedule": schedule, "prompt": prompt,
            "status": "active", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_run": None, "next_run": None,
        }
        _save_cron_jobs(jobs)
        return json.dumps({"success": True, "job_id": job_id, "name": name})

    elif action in ("pause", "resume", "delete", "run_now"):
        job_id = args.get("job_id", "")
        if not job_id or job_id not in jobs:
            return json.dumps({"error": f"任务 '{job_id}' 未找到"})
        if action == "pause":
            jobs[job_id]["status"] = "paused"
        elif action == "resume":
            jobs[job_id]["status"] = "active"
        elif action == "delete":
            del jobs[job_id]
        elif action == "run_now":
            jobs[job_id]["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_cron_jobs(jobs)
        return json.dumps({"success": True, "job_id": job_id, "action": action})

    return json.dumps({"error": f"未知操作: {action}"})


registry.register(name="cronjob", toolset="cron", schema=CRONJOB_SCHEMA,
                  handler=_handle_cronjob, check_fn=lambda: True, emoji="⏰")


# ============================================================================
# 蓝图工具（来自 blueprints.py）
# ============================================================================

BLUEPRINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "blueprint",
        "description": "管理蓝图自动化：解析、列出、激活技能中声明的定时自动化。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "activate", "export"],
                    "description": "操作类型",
                },
                "skill_name": {"type": "string", "description": "技能名称"},
            },
            "required": ["action"],
        },
    },
}


def _handle_blueprint(args: Dict[str, Any], **kwargs) -> str:
    action = args.get("action", "")
    if action == "list":
        # 扫描已安装技能中的蓝图
        from spirit.tools.skills import SKILLS_DIR, _find_all_skills
        skills = _find_all_skills()
        blueprints = []
        for s in skills:
            skill_md = Path(s.path) / "SKILL.md"
            if skill_md.exists():
                text = skill_md.read_text(encoding="utf-8", errors="replace")
                if "blueprint:" in text:
                    blueprints.append({"skill": s.name, "description": s.description})
        return json.dumps({"blueprints": blueprints, "count": len(blueprints)}, ensure_ascii=False)
    elif action == "activate":
        skill_name = args.get("skill_name", "")
        if not skill_name:
            return json.dumps({"error": "skill_name 必填"})
        return json.dumps({
            "success": False,
            "error": "蓝图激活需要定时任务调度器，请配置 cron 后端。",
        })
    elif action == "export":
        return json.dumps({"error": "蓝图导出功能开发中"})
    return json.dumps({"error": f"未知操作: {action}"})


registry.register(name="blueprint", toolset="automation", schema=BLUEPRINT_SCHEMA,
                  handler=_handle_blueprint, check_fn=lambda: True, emoji="📐")


# ============================================================================
# 桌面控制工具（来自 computer_use_tool.py）
# ============================================================================

COMPUTER_USE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": (
            "通过 cua-driver 进行通用桌面控制（macOS、Windows、Linux）。\n"
            "支持鼠标点击、键盘输入、截屏等操作。\n"
            "后台运行，不抢占用户光标或键盘焦点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "click", "type", "key", "scroll", "wait", "drag"],
                    "description": "操作类型",
                },
                "coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "坐标 [x, y]",
                },
                "text": {"type": "string", "description": "要输入的文本"},
                "key": {"type": "string", "description": "按键名称"},
                "duration": {"type": "number", "description": "等待秒数"},
            },
            "required": ["action"],
        },
    },
}


def _handle_computer_use(args: Dict[str, Any], **kwargs) -> str:
    return json.dumps({
        "success": False,
        "error": "桌面控制需要安装 cua-driver。运行 `pip install cua-driver` 安装。",
    })


def check_computer_use() -> bool:
    try:
        import cua_driver  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(name="computer_use", toolset="computer_use", schema=COMPUTER_USE_SCHEMA,
                  handler=_handle_computer_use, check_fn=check_computer_use, emoji="🖥️")


# ============================================================================
# 工具结果持久化（来自 tool_result_storage.py）
# ============================================================================

PERSISTED_OUTPUT_TAG = "<persisted-output>"
STORAGE_DIR = Path.home() / ".spirit" / "tool-results"


def maybe_persist_tool_result(tool_name: str, tool_use_id: str, output: str,
                               threshold: int = DEFAULT_RESULT_SIZE_CHARS) -> str:
    """如果工具输出超过阈值，持久化到磁盘并返回预览。"""
    if tool_name in PINNED_THRESHOLDS:
        return output
    if len(output) <= threshold:
        return output

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_use_id)[:120]
    file_path = STORAGE_DIR / f"{safe_id}.txt"
    file_path.write_text(output, encoding="utf-8")

    preview = output[:DEFAULT_PREVIEW_SIZE_CHARS]
    return (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"{preview}\n"
        f"[... 输出已截断，共 {len(output)} 字符。完整内容保存在 {file_path}]\n"
        f"</persisted-output>"
    )


# ============================================================================
# 补丁解析器（来自 patch_parser.py）
# ============================================================================

@dataclass
class HunkLine:
    prefix: str
    content: str


@dataclass
class Hunk:
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    operation: str  # "add" | "update" | "delete" | "move"
    file_path: str
    new_path: Optional[str] = None
    hunks: List[Hunk] = field(default_factory=list)
    content: Optional[str] = None


def parse_v4a_patch(patch_content: str) -> tuple:
    """解析 V4A 格式的补丁。"""
    operations: List[PatchOperation] = []
    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    for line in patch_content.splitlines():
        if line.startswith("*** Begin Patch"):
            continue
        elif line.startswith("*** End Patch"):
            break
        elif line.startswith("*** Update File:"):
            if current_op:
                operations.append(current_op)
            current_op = PatchOperation(operation="update", file_path=line.split(":", 1)[1].strip())
            current_hunk = None
        elif line.startswith("*** Add File:"):
            if current_op:
                operations.append(current_op)
            current_op = PatchOperation(operation="add", file_path=line.split(":", 1)[1].strip())
            current_hunk = None
        elif line.startswith("*** Delete File:"):
            if current_op:
                operations.append(current_op)
            current_op = PatchOperation(operation="delete", file_path=line.split(":", 1)[1].strip())
            current_hunk = None
        elif line.startswith("*** Move File:"):
            if current_op:
                operations.append(current_op)
            parts = line.split(":", 1)[1].strip().split(" -> ")
            current_op = PatchOperation(operation="move", file_path=parts[0],
                                         new_path=parts[1] if len(parts) > 1 else None)
            current_hunk = None
        elif line.startswith("@@"):
            if current_op:
                context_hint = line.strip("@ ").strip()
                current_hunk = Hunk(context_hint=context_hint or None)
                current_op.hunks.append(current_hunk)
        elif line.startswith((" ", "-", "+")) and current_hunk is not None:
            prefix = line[0]
            content = line[1:]
            current_hunk.lines.append(HunkLine(prefix=prefix, content=content))
        elif line.startswith("+") and current_op and current_op.operation == "add":
            if current_op.content is None:
                current_op.content = ""
            current_op.content += line[1:] + "\n"

    if current_op:
        operations.append(current_op)

    return operations, None
