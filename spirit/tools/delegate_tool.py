"""子 Agent 委派工具 — delegate_task（并行子任务）。

参考 Hermes 的 tools/delegate_tool.py 设计：
- 生成子 Agent 处理独立子任务
- 支持并行执行多个子任务
- 子 Agent 有独立上下文，结果汇总回父 Agent
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

# 子 Agent 不允许使用的工具
BLOCKED_TOOLS_FOR_CHILDREN = frozenset([
    "delegate_task",   # 禁止递归委派
    "clarify",         # 子 Agent 不能与用户交互
    "memory",          # 子 Agent 不写共享记忆
])

DEFAULT_TIMEOUT = get_config_value("timeouts.delegate_default", 900)  # 单次尝试墙钟上限


# ---------------------------------------------------------------------------
# 子 Agent 进度上报（桌面 CLI 实时视图）
# ---------------------------------------------------------------------------
# ws_server 启动时注册 sink；子 Agent 线程内的工具/思考/生成事件经 sink
# 广播为 delegate_event，CLI 终端以带名称前缀的树状行 + 底部状态块呈现。
_progress_sink: Optional[Callable[[dict], None]] = None


def set_delegate_progress_sink(sink: Optional[Callable[[dict], None]]) -> None:
    """注册子 Agent 进度事件 sink（无 sink 时零行为变化）。"""
    global _progress_sink
    _progress_sink = sink


def _emit_progress(evt: Dict[str, Any]) -> None:
    sink = _progress_sink
    if sink is None:
        return
    try:
        sink(evt)
    except Exception:  # 进度上报失败绝不影响主任务
        pass


def _goal_snippet(goal: str, limit: int = 40) -> str:
    """任务目标的首句摘要，用作子 Agent 名称后缀展示。"""
    flat = " ".join((goal or "").split())
    if not flat:
        return "(未命名任务)"
    for sep in ("。", "！", "？", "!", "?", "；", ";"):
        idx = flat.find(sep)
        if 0 < idx < len(flat):
            flat = flat[: idx + 1]
            break
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------------------
# 活跃子 Agent 注册表 + 任务快照（chat 超时点杀 / 续跑指令重注入）
# ---------------------------------------------------------------------------
_live_lock = threading.RLock()
_live_children: Dict[str, Dict[str, Any]] = {}  # child_id -> {"agent", "meta"}
_snapshot_lock = threading.RLock()
_delegate_snapshot: Optional[Dict[str, Any]] = None  # {"updated": ts, "tasks": [...]}


def _snapshot_init(tasks: List[Dict], metas: List[Dict]) -> None:
    global _delegate_snapshot
    with _snapshot_lock:
        _delegate_snapshot = {
            "updated": time.time(),
            "tasks": [
                {
                    "name": m["name"], "goal": t.get("goal", ""),
                    "context": t.get("context", ""), "status": "running",
                }
                for t, m in zip(tasks, metas)
            ],
        }


def _snapshot_update(name: str, status: str) -> None:
    with _snapshot_lock:
        if not _delegate_snapshot:
            return
        for t in _delegate_snapshot["tasks"]:
            if t["name"] == name:
                t["status"] = status
                _delegate_snapshot["updated"] = time.time()
                break


def get_unfinished_delegate_tasks(max_age_seconds: float = 1800) -> List[Dict[str, str]]:
    """最近一次委派中未成功子任务清单（供超时续跑指令重注入重启子任务）。"""
    with _snapshot_lock:
        snap = _delegate_snapshot
        if not snap or time.time() - snap["updated"] > max_age_seconds:
            return []
        return [dict(t) for t in snap["tasks"] if t["status"] != "success"]


def interrupt_live_children(reason: str = "") -> int:
    """中断所有运行中子 Agent（chat 超时/外部取消时调用）；被杀尝试不重启。"""
    with _live_lock:
        entries = list(_live_children.values())
    for entry in entries:
        entry["meta"]["external_kill"] = reason or "父任务中断"
        try:
            entry["agent"].interrupt()
        except Exception:
            logger.debug("子 Agent interrupt 失败", exc_info=True)
    return len(entries)


def _stall_monitor(meta: Dict, child: Any, stop_evt: threading.Event) -> None:
    """心跳停滞监控：进展事件长期断供判停滞，杀当前尝试（attempt 循环决定是否重启）。"""
    idle_lim = float(get_config_value("delegation.child_stale_idle_seconds", 450))
    tool_lim = float(get_config_value("delegation.child_stale_in_tool_seconds", 1200))
    while not stop_evt.wait(30):
        gap = time.time() - (meta.get("last_progress") or time.time())
        lim = tool_lim if meta.get("in_tool") else idle_lim
        if lim > 0 and gap > lim:
            meta["stall_reason"] = (
                f"停滞 {gap:.0f}s 无进展（限 {lim:.0f}s，"
                f"{'工具执行中' if meta.get('in_tool') else '轮间等待'}）"
            )
            try:
                child.interrupt()
            except Exception:
                pass
            return


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "委派子任务给独立的子 Agent 执行。\n\n"
            "适用场景：\n"
            "- 多个独立任务可并行处理\n"
            "- 需要隔离上下文避免干扰\n"
            "- 复杂任务分解为子任务\n\n"
            "子 Agent 拥有与父 Agent 相同的工具集（除委派/交互/记忆外）。\n"
            "父 Agent 的上下文只看到委派请求和汇总结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "子任务列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "任务目标描述"},
                            "context": {"type": "string", "description": "相关上下文"},
                        },
                        "required": ["goal"],
                    },
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"每个子任务单次尝试的墙钟上限秒数（默认 {DEFAULT_TIMEOUT}）；"
                        "超时/停滞/异常会自动重启子任务重试"
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}


def _wire_child_progress(child: Any, meta: Dict[str, str]) -> None:
    """把子 Agent 的回调接成进度事件（轮次/工具/思考/生成字数）。"""
    st = {
        "in_turn": False, "turn": 0, "gen": 0,
        "last_gen_emit": 0.0, "think_buf": "", "think_sent": False,
        "tool_started_at": 0.0,
    }

    def _preview(args: Any) -> str:
        try:
            if isinstance(args, dict):
                for v in args.values():
                    if isinstance(v, str) and v.strip():
                        return " ".join(v.split())[:60]
            return " ".join(str(args).split())[:60]
        except Exception:
            return ""

    def _on_tool_start(name: str, args: Any = None) -> None:
        st["in_turn"] = False
        st["think_sent"] = False
        st["think_buf"] = ""
        st["tool_started_at"] = time.time()
        meta["last_progress"] = time.time()
        meta["in_tool"] = True
        _emit_progress({
            "phase": "child_tool", "id": meta["id"], "name": meta["name"],
            "tool": name, "preview": _preview(args),
        })

    def _on_tool_complete(name: str, result: Any = None) -> None:
        dur = round(time.time() - st["tool_started_at"], 1) if st["tool_started_at"] else 0.0
        ok = True
        if isinstance(result, str):
            head = result.lstrip()[:200]
            ok = not (head.startswith("{") and '"error"' in head)
        meta["last_progress"] = time.time()
        meta["in_tool"] = False
        _emit_progress({
            "phase": "child_tool_done", "id": meta["id"], "name": meta["name"],
            "tool": name, "ok": ok, "duration": dur,
        })

    def _on_stream(text: str) -> None:
        if not text:
            return
        meta["last_progress"] = time.time()
        if not st["in_turn"]:
            st["in_turn"] = True
            st["turn"] += 1
            st["think_buf"] = ""
            st["think_sent"] = False
            _emit_progress({
                "phase": "child_turn", "id": meta["id"], "name": meta["name"],
                "turn": st["turn"],
            })
        st["gen"] += len(text)
        if not st["think_sent"]:
            st["think_buf"] += text
            if len(st["think_buf"]) >= 60:
                st["think_sent"] = True
                _emit_progress({
                    "phase": "child_think", "id": meta["id"], "name": meta["name"],
                    "text": " ".join(st["think_buf"].split())[:40],
                })
        now = time.time()
        if now - st["last_gen_emit"] >= 1.0:
            st["last_gen_emit"] = now
            _emit_progress({
                "phase": "child_gen", "id": meta["id"], "name": meta["name"],
                "chars": st["gen"], "turn": st["turn"],
            })

    child.on_tool_start = _on_tool_start
    child.on_tool_complete = _on_tool_complete
    child.on_stream_delta = _on_stream


def _run_attempt(
    task: Dict, meta: Dict, prompt: str, attempt: int, cap: float,
):
    """跑单次尝试，返回 (ok, result, fail_reason)。

    停滞/单次墙钟超限/异常都算失败，由调用方（attempt 循环）决定重启；
    external_kill（父级中断：chat 超时等）直接收敛不重启。超限尝试的
    工作线程为 daemon 遗弃：interrupt 后在迭代边界自然退出，不阻塞进程。
    """
    from spirit.agent.agent import AgentConfig, SpiritAgent

    child_id = meta["id"]
    config = AgentConfig(
        session_id=f"child-{child_id}-a{attempt}",
        platform="delegate",
        max_iterations=30,  # 子 Agent 迭代次数限制
    )
    child = SpiritAgent(config)
    _wire_child_progress(child, meta)
    meta["attempt"] = attempt
    meta["last_progress"] = time.time()
    meta["in_tool"] = False
    meta["stall_reason"] = None
    with _live_lock:
        _live_children[child_id] = {"agent": child, "meta": meta}

    stop_evt = threading.Event()
    mon = threading.Thread(
        target=_stall_monitor, args=(meta, child, stop_evt), daemon=True,
    )
    mon.start()

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["res"] = child.run_conversation(prompt)
        except BaseException as exc:  # noqa: BLE001 - 交 attempt 循环分类
            box["err"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(cap if cap > 0 else None)
    stop_evt.set()

    with _live_lock:
        _live_children.pop(child_id, None)

    if worker.is_alive():
        meta["stall_reason"] = f"单次尝试超墙钟 {cap:.0f}s"
        try:
            child.interrupt()
        except Exception:
            pass
        return False, None, meta["stall_reason"]
    if meta.get("external_kill"):
        return False, None, f"被中断: {meta['external_kill']}"
    if "err" in box:
        return False, None, str(box["err"])[:200]
    if meta.get("stall_reason"):
        return False, box.get("res"), meta["stall_reason"]
    res = box.get("res")
    if res is None:
        return False, None, "子 Agent 无返回值"
    return True, res, None


def _delegate_task_impl(
    tasks: List[Dict[str, str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """委派子任务（停滞/单次超时/异常自动重启子任务）。"""
    if not tasks:
        return json.dumps({"error": "至少需要一个子任务"})

    if len(tasks) > 5:
        return json.dumps({"error": "最多同时委派 5 个子任务"})

    timeout = min(max(timeout, 60), 3600)  # 单次尝试墙钟上限
    max_attempts = max(1, int(get_config_value("delegation.child_max_attempts", 2)))
    results = []
    batch_started = time.time()

    # 预分配子 Agent 身份（名称 + id），全部进度事件携带
    metas = [
        {
            "id": str(uuid.uuid4())[:8],
            "name": f"子Agent-{i + 1}",
            "goal": _goal_snippet(task.get("goal", "")),
            "attempt": 1, "restarts": 0,
            "last_progress": 0.0, "in_tool": False,
            "stall_reason": None, "external_kill": None,
        }
        for i, task in enumerate(tasks)
    ]
    _snapshot_init(tasks, metas)
    _emit_progress({
        "phase": "start",
        "children": [
            {"id": m["id"], "name": m["name"], "goal": m["goal"]} for m in metas
        ],
        "max_attempts": max_attempts,
    })

    def _run_child(task: Dict, meta: Dict) -> Dict:
        """运行单个子任务（attempt 循环：失败自动重启）。"""
        child_id = meta["id"]
        goal = task.get("goal", "")
        context = task.get("context", "")
        prompt = f"上下文:\n{context}\n\n任务: {goal}" if context else goal

        started = time.time()
        last_reason = ""
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            ok, res, reason = _run_attempt(
                task, meta, prompt, attempt, float(timeout),
            )
            if ok:
                duration = time.time() - started
                _snapshot_update(meta["name"], "success")
                _emit_progress({
                    "phase": "child_done", "id": child_id, "name": meta["name"],
                    "success": True, "iterations": res.iterations,
                    "duration": round(duration, 1), "attempts": attempt,
                    "snippet": " ".join((res.response or "").split())[:60],
                })
                return {
                    "task_id": child_id,
                    "goal": goal,
                    "success": True,
                    "response": res.response[:2000],  # 截断
                    "iterations": res.iterations,
                    "duration": round(duration, 1),
                    "attempts": attempt,
                }
            last_reason = reason or "未知失败"
            if meta.get("external_kill"):
                break  # 父级中断（chat 超时等）：只收敛不重启
            if attempt < max_attempts:
                meta["restarts"] += 1
                _snapshot_update(meta["name"], "restarting")
                logger.warning(
                    "子任务 %s 第 %d/%d 次尝试失败（%s），自动重启",
                    meta["name"], attempt, max_attempts, last_reason,
                )
                _emit_progress({
                    "phase": "child_restart", "id": child_id, "name": meta["name"],
                    "attempt": attempt + 1, "reason": last_reason[:80],
                })

        _snapshot_update(
            meta["name"],
            "interrupted" if meta.get("external_kill") else "failed",
        )
        _emit_progress({
            "phase": "child_done", "id": child_id, "name": meta["name"],
            "success": False, "error": last_reason[:120], "attempts": attempt,
        })
        return {
            "task_id": child_id,
            "goal": goal,
            "success": False,
            "error": last_reason,
            "attempts": attempt,
        }

    # 并行执行；收集兜底 = 单次上限 × 尝试次数 + 余量，命中则点杀该子再收一次
    collect_timeout = timeout * max_attempts + 120
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {
            executor.submit(_run_child, task, meta): i
            for i, (task, meta) in enumerate(zip(tasks, metas))
        }

        for future in futures:
            idx = futures[future]
            meta = metas[idx]
            try:
                results.append(future.result(timeout=collect_timeout))
            except FuturesTimeoutError:
                with _live_lock:
                    entry = _live_children.get(meta["id"])
                if entry:
                    meta["external_kill"] = "委派收集兜底超时"
                    try:
                        entry["agent"].interrupt()
                    except Exception:
                        pass
                try:
                    results.append(future.result(timeout=90))
                except Exception:
                    results.append({
                        "task_id": meta["id"],
                        "goal": tasks[idx].get("goal", ""),
                        "success": False,
                        "error": f"超时（{timeout}s/尝试 × {max_attempts} 次）",
                    })
            except Exception as e:
                results.append({
                    "task_id": meta["id"],
                    "goal": tasks[idx].get("goal", ""),
                    "success": False,
                    "error": str(e),
                })

    _emit_progress({
        "phase": "end",
        "succeeded": len([r for r in results if r.get("success")]),
        "failed": len([r for r in results if not r.get("success")]),
        "restarted": sum(m["restarts"] for m in metas),
        "duration": round(time.time() - batch_started, 1),
    })

    # 汇总
    summary = {
        "total": len(tasks),
        "succeeded": len([r for r in results if r.get("success")]),
        "failed": len([r for r in results if not r.get("success")]),
        "results": results,
    }

    return json.dumps(summary, ensure_ascii=False, indent=2)


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_SCHEMA,
    handler=_delegate_task_impl,
    description="委派子任务",
    emoji="🔀",
    max_result_size_chars=get_config_value("limits.delegate_max_chars", 20_000),
)
