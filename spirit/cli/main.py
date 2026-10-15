"""Spirit Agent CLI — 命令行入口。

用法：
    spirit chat          交互式对话
    spirit start         启动后端服务
    spirit sessions      查看会话历史
    spirit tools         查看可用工具
"""

import logging
import os
import sys
import time
# v0.3.4 fix: 显示时区换算（数据库 started_at/ended_at 是 UTC ISO 字符串，
# SQLite CURRENT_TIMESTAMP 永远 UTC。显示时必须 + 本地偏移，否则少 8h）。
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text


def _fmt_local_ts(ts_str, length: int = 19) -> str:
    """格式化时间戳显示。 v0.3.4：数据库 default 改为
    ``datetime('now', 'localtime')`` 后存的是本地时间，**不再 + 偏移**。
    保留本函数仅用于：
    1. 裁剪多余精度（毫秒/微秒）
    2. 容错（异常时返回原字符串）
    3. 末来如果检测到看起来像 UTC 的 “明天” 字符串，防御性 + 偏移
    """
    if not ts_str:
        return ""
    raw = str(ts_str)[:length]
    try:
        # 判断是 datetime 还是纯日期
        if len(raw) >= 19 and raw[10:11] in (" ", "T") and ":" in raw:
            return raw  # 已经是 YYYY-MM-DD HH:MM:SS 直接返回
        return raw
    except Exception:
        return raw

# ── 阶段进度事件总线（C-P1.1 接线）──
# spirit.conversation_loop 在 9 个点调 notify_progress() 广播 7 阶段
# （task_start/thinking/thinking_end/tool_call/tool_result/task_complete/task_aborted）；
# 这里注册一个 sink 把这些事件渲染到终端，让 CLI 能看到工具调用、思考耗时、
# 任务阶段、最终结果汇总（对齐 Hermes 的工具调用摘要 + 分阶段显示）。
from spirit.agent.progress_notifier import (
    ProgressPhase,
    register_progress_sink,
    unregister_progress_sink,
)

console = Console()


# ---------------------------------------------------------------------------
# CLI 渲染辅助 — 顶部欢迎面板 + 历史会话 + 底部状态栏
# ---------------------------------------------------------------------------
# 设计语言:
#   - 主色: yellow (Spirit 小狐狸主题)
#   - 折叠: dim 灰色边框 + 小标题
#   - 顶部欢迎面板: HEAVY 边框 + 🦊 图标
#   - 状态栏: 'state | model | ctx% | Σ 用量 | 时长 | msgs | tools' + 绿点
#     （用 FormattedText 片段，不用 Rich markup 字符串—prompt_toolkit HTML() 不解析 markup）
#   - 历史任务: [n] 编号 + 第一条用户消息前 50 字
#   - 阶段渲染: 思考中 Live(Spinner) + 完成后逐行阶段标题 + 工具调用摘要
# ---------------------------------------------------------------------------

WELCOME_PANEL = """\
[bold yellow]🦊 Spirit Agent CLI v1.0[/bold yellow]

[white]输入[/white] [bold]/help[/bold] [white]查看可用命令[/white]
[white]直接输入文字与[/white] [bold]Agent[/bold] [white]对话[/white]"""

RECENT_HEADER = Text()
RECENT_HEADER.append("—— 最近任务（点击编号或输入 ", style="dim")
RECENT_HEADER.append("[n]", style="yellow")
RECENT_HEADER.append(" 恢复并继续，/tasks 重显）——", style="dim")

# 状态栏片段 style key（在 chat() 里 Style.from_dict 集中注册）
STATUS_STYLE = {
    "status-bar":        "ansiyellow",                       # 主色：主文
    "status-bar-dim":    "ansigray",                          # 灰色：分隔符 / 标签
    "status-bar-strong": "ansiyellow bold",                   # 强亮黄：状态
    "status-bar-warn":   "ansiyellow",                        # 黄：ctx 中位
    "status-bar-bad":    "ansired bold",                      # 红：ctx 高位
    "status-bar-good":   "ansigreen",                         # 绿：ctx 低位
    "status-bar-thinking": "ansiyellow bold",                  # 思考中提示
    "status-bar-tool":   "ansicyan",                          # 工具/用量计数
}


def _fmt_duration(secs: float) -> str:
    """人性化时长：0s / 12s / 3m25s / 1h12m。"""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _fmt_tokens(n: int) -> str:
    """人性化 token 计数：0 / 999 / 1.2K / 23K / 1.5M / 12M。"""
    if n is None or n <= 0:
        return "0"
    n = int(n)
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return f"{n / 1_000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1_000)}K"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{round(n / 1_000_000)}M"


def _truncate(s: str, n: int) -> str:
    """安全截断中英文混合串。"""
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _fold_summary_single_line(s: Any, max_len: int = 70) -> str:
    """折叠 summary 为单行（避免 task_start 多行渲染后让用户以为是“重复块”）。

    策略：
    - 空 → ""
    - 不含 ``\\n`` → 照原值，仅过 _truncate(max_len)
    - 含 ``\\n`` → 只保留首行，如果总长超出 max_len 追加 ``... (+N 行)`` 提示
    """
    if not s:
        return ""
    text = str(s)
    if "\n" not in text:
        return _truncate(text, max_len)
    first, _, rest = text.partition("\n")
    first = first.rstrip()
    rest_lines = [ln for ln in rest.split("\n") if ln.strip()]
    n_more = len(rest_lines)
    head = _truncate(first, max_len - 12)  # 预留 "… (+N 行)" 位置
    if n_more == 0:
        return head + ("…" if len(first) > len(head) else "")
    return f"{head}… (+{n_more} 行)"


def _format_tool_call_summary(tool_calls: List[Dict[str, Any]], max_args: int = 3) -> str:
    """拼接工具调用摘要：函数名 + 简化参数（参数 > max_args 个时标 +N more）。

    参考 Hermes CLI 工具调用显示规范：函数名 + 简化参数。
    """
    if not tool_calls:
        return ""
    parts: List[str] = []
    for tc in tool_calls:
        func = (tc.get("function") or {})
        name = func.get("name", "?") or "?"
        raw_args = func.get("arguments", "")
        # 参数可能是 JSON 字符串，也可能已经是 dict
        if isinstance(raw_args, dict):
            args_dict = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                import json as _json
                args_dict = _json.loads(raw_args)
            except Exception:
                args_dict = {"_": _truncate(raw_args, 30)}
        else:
            args_dict = {}
        # 取前 max_args 个参数
        items = list(args_dict.items())[:max_args]
        kv = ", ".join(f"{k}={_truncate(str(v), 24)}" for k, v in items)
        more = len(args_dict) - max_args
        suffix = f", +{more} more" if more > 0 else ""
        if kv:
            parts.append(f"{name}({kv}{suffix})")
        else:
            parts.append(f"{name}()")
    if len(parts) > 4:
        return "、".join(parts[:4]) + f" 等 {len(parts)} 个"
    return "、".join(parts)


# v0.4.x：新增 3 个工具显示辅助 — 对齐 Hermes cli_stream_mixin 风格
# - _format_tool_args : dict → 单行 ``k=v, k=v`` 预览（与桌宠一致）
# - _format_tool_result : 字符串 → 折叠截断（多行换行 + 长度上限）
# - _format_cute_tool_message : 单行 cute 文案（emoji + 工具名 + 摘要 + 耗时）

def _format_tool_args(args: Any, max_len: int = 200) -> str:
    """格式化工具参数为单行 ``k=v, k=v`` 预览。

    - 嵌套结构：dict / list 会被 str() 后拼接
    - 过长按 ``max_len`` 截断 + ``...``
    - 空 / 非 dict → ``""``（由调用方决定是否打印括号）
    """
    if not args:
        return ""
    if isinstance(args, dict):
        parts: List[str] = []
        for k, v in args.items():
            v_str = "***" if v == "***" else str(v)
            if len(v_str) > 32:
                v_str = v_str[:31] + "…"
            parts.append(f"{k}={v_str}")
        s = ", ".join(parts)
    else:
        s = str(args)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _format_tool_result(result: Any, max_len: int = 300) -> str:
    """把工具结果字符串折叠到 ``max_len`` 字符以内。

    策略：
    - 多行时只保留首行 + ``…``
    - 单行过长时尾部截断 + ``…``
    - 非字符串（list / dict）→ 先 str() 再截断
    - 空 → ``""``
    """
    if result is None:
        return ""
    if not isinstance(result, str):
        try:
            import json as _json
            result = _json.dumps(result, ensure_ascii=False)
        except Exception:
            result = str(result)
    if not result:
        return ""
    # 多行折叠：只显示首行
    if "\n" in result:
        first_line = result.split("\n", 1)[0].rstrip()
        if len(first_line) > max_len:
            return first_line[: max_len - 1] + "…"
        rest_len = len(result) - len(first_line)
        return f"{first_line} …(+{rest_len} chars)"
    if len(result) > max_len:
        return result[: max_len - 1] + "…"
    return result


_CUTE_TOOL_EMOJI = {
    "read_file": "\U0001f4c4",        # 📄
    "write_file": "\U0001f4dd",       # 📝
    "edit_file": "\U0001f58a",        # 🖊
    "terminal": "\U0001f4bb",         # 💻
    "execute_code": "\U0001f680",     # 🚀
    "search_files": "\U0001f50d",     # 🔍
    "grep": "\U0001f50d",
    "web_search": "\U0001f310",       # 🌐
    "web_fetch": "\U0001f310",
    "delegate_task": "\U0001f465",    # 👥
    "todo": "\U0001f4cb",             # 📋
    "image_analysis": "\U0001f5bc",   # 🖼
    "memora_search": "\U0001f4da",    # 📚
    "memora_save": "\U0001f4be",      # 💾
}
_DEFAULT_TOOL_EMOJI = "\u26a1"        # ⚡


def _format_cute_tool_message(
    name: str,
    args: Any = None,
    duration_ms: Optional[int] = None,
    result: Any = None,
    is_error: bool = False,
) -> str:
    """单行 cute 工具消息（emoji + 工具名 + 摘要 + 耗时 + 状态）。

    对齐 Hermes ``get_cute_tool_message(name, args, duration, result=...)`` 字段；
    spirit 版额外附 is_error 时换红色 ⚠️。
    """
    emoji = _CUTE_TOOL_EMOJI.get(name, _DEFAULT_TOOL_EMOJI)
    status_icon = "⚠️ " if is_error else "✓ "
    parts: List[str] = [f"{emoji} {status_icon}{name}"]
    arg_preview = _format_tool_args(args, max_len=120)
    if arg_preview:
        parts.append(f"({arg_preview})")
    if duration_ms is not None:
        parts.append(f"({duration_ms / 1000:.1f}s)")
    if result is not None and not is_error:
        result_preview = _format_tool_result(result, max_len=120)
        if result_preview:
            parts.append(f"→ {result_preview}")
    return " ".join(parts)


# v0.4.x：最终回答流式 box 管理（对齐 _ReasoningStream）
# STREAM_TEXT_DELTA 逐 token 到达时累积到 buffer；
# STREAM_TEXT_END 时按自然边界 flush + 折叠。
# 单一职责：管理一个流式回答的渲染生命周期，避免重复 box 互相覆盖。


# ---------------------------------------------------------------------------
# 阶段进度事件渲染（register_progress_sink 的 sink 回调）
# ---------------------------------------------------------------------------
# 在 spirit.cli.chat() 里调 register_progress_sink(_render_progress) 生效；
# conversation_loop 推送 7 阶段，本函数负责逐阶段渲染到 console（不在
# Live(Spinner) 里渲染——避免 spinner 刷新覆盖阶段标题、阶段行产生越级）。
# ---------------------------------------------------------------------------


class _ReasoningStream:
    """推理内容流式渲染缓冲。

    REASONING_DELTA 事件按 token 粒度到达（一个词、一个标点都可能一条）。
    直接逐条打印会刷屏——模仿 Hermes 的 _flush_reasoning_preview：累积到
    自然边界（换行 / 句末标点 / >80 字符）才 emit 一行，REASONING_END 时 force flush。

    v0.4.x：升级为 box 框架（对齐 Hermes cli_stream_mixin._stream_reasoning_delta）：
    - 第一次 feed() 时打印 ``┌─ Reasoning ─...─┐`` 顶部
    - REASONING_END / close_box() 时打印 ``└─...─┘`` 底部
    - STREAM_TEXT_DELTA 触发时协同关闭（响应到达立即收 reasoning box）
    - 内部行不再用 💭 emoji（box label 已是标识）

    设计：
    - 轮次间不重置 buffer 会有跨轮泄露问题——在 REASONING_END + THINKING_END 双重信号
      下清空，避免中途断连丢尾巴。
    - 折叠输出：超过 5 行只显示前 5 行 + ``... (+N more lines)``（对齐 Hermes ``reasoning_full=False``）。
    - 单例：模块级 _reasoning_stream，所有 REASONING_DELTA 事件共用一个 buffer。
    """

    # v0.4.x：box 顶部/底部宽度（对齐 Hermes scrollback box）
    _BOX_WIDTH = 80

    def __init__(self) -> None:
        self._buf: str = ""
        self._active: bool = False  # 当前是否在收 reasoning delta
        self._emitted_lines: int = 0  # 本轮已打印的行数（用于折叠）
        self._printed_full: bool = False  # 本轮是否已打印折叠后的完整 preview（避免 REASONING_END 重复打印）
        # v0.4.x：box 状态（对齐 Hermes _reasoning_box_opened）
        self._box_opened: bool = False  # reasoning box 是否已打开

    def reset(self) -> None:
        """轮次开始时调用。"""
        self._buf = ""
        self._active = True
        self._emitted_lines = 0
        self._printed_full = False
        # v0.4.x：上一轮 box 如果未关（异常路径），强制关掉避免泄露
        if self._box_opened:
            self._emit_box_bottom()
        self._box_opened = False

    def feed(self, piece: str) -> Optional[str]:
        """累积一个增量。返回需要立即打印的一行（可能有，也可能没有）。"""
        if not self._active or not piece:
            return None
        # v0.4.x：第一次 feed 时打开 reasoning box（对齐 Hermes _stream_reasoning_delta）
        if not self._box_opened:
            self._box_opened = True
            self._emit_box_top()
        self._buf += piece
        # 自然边界：换行 / 句末标点 / >80 字符强制 flush
        target_width = 80
        if "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._emitted_lines += 1
                return line
        elif len(self._buf) >= target_width:
            # 强制按词边界切
            cut = max(
                self._buf.rfind(b)
                for b in (" ", "\t", ".", "!", "?", ",", ";", ":")
            )
            if cut != -1 and cut >= target_width // 2:
                line, self._buf = self._buf[: cut + 1], self._buf[cut + 1 :]
                self._emitted_lines += 1
                return line
        return None

    def close_box(self) -> None:
        """关闭 reasoning box（响应 box 打开时 / REASONING_END 时调用）。

        对齐 Hermes cli_stream_mixin._close_reasoning_box:
        - 刷出剩余 buffer
        - 打印底部 ┌─...─┐
        - 重置 _box_opened 状态
        """
        if not self._box_opened:
            return
        # 刷尾部剩余
        tail = self._buf.strip()
        if tail:
            console.print(f"  [dim]{tail}[/dim]", highlight=False)
        self._buf = ""
        self._emit_box_bottom()
        self._box_opened = False

    def flush(self) -> Optional[str]:
        """轮次结束时调用——刷出尾部剩余 + 返回可选的折叠提示行。

        v0.4.x：同时关闭 box（调 close_box）。
        """
        if not self._active:
            return None
        self._active = False
        # v0.4.x：关闭 box 框架（之前 box_opened=True 现在会折叠，所以需关掉）
        # 只在还有未刷尾 buffer 时关 box——避免空 box 打印
        try:
            self.close_box()
        except Exception:
            pass
        tail = self._buf.strip()
        self._buf = ""
        if not tail:
            return None
        self._emitted_lines += 1
        # 注意：本轮不再打印 tail 本身（已在中途分批发完或根本没收到），
        # 这里只返回可能的折叠提示。
        return None

    def _emit_box_top(self) -> None:
        """打印 reasoning box 顶部 ┌─ Reasoning ─...─┐（对齐 Hermes scrollback box）。"""
        w = self._BOX_WIDTH
        label = " Reasoning "
        fill = w - 2 - len(label)
        if fill < 0:
            fill = 0
        console.print(
            f"  [dim]┌─{label}{'─' * fill}┐[/dim]",
            highlight=False,
        )

    def _emit_box_bottom(self) -> None:
        """打印 reasoning box 底部 └─...─┘。"""
        w = self._BOX_WIDTH
        console.print(
            f"  [dim]└{'─' * (w - 2)}┘[/dim]",
            highlight=False,
        )

    def fold_preview(self) -> Optional[str]:
        """轮结束后想重看完整 reasoning preview 时调用——返回 ``... (+N more lines)`` 之类的提示。

        本函数暂未使用，预留给 /verbose 之类的子命令。
        """
        if self._emitted_lines <= 5:
            return None
        return f"... (+{self._emitted_lines - 5} more lines)"


class _StreamTextBox:
    """最终回答流式渲染缓冲（v0.4.x，对齐 _ReasoningStream）。

    职责：
    - STREAM_TEXT_DELTA 事件累积到 buffer（逐 chunk 到达）
    - 按自然边界（换行 / 句末标点 / >120 字符）flush 一行到 console
    - STREAM_TEXT_END 时刷出尾部剩余
    - 重复 box / 跨轮泄漏防护：active 标志 + reset() 钩子

    与 _ReasoningStream 的差异：
    - 触发源不同：REASONING_DELTA（reasoning_content）vs STREAM_TEXT_DELTA（content）
    - 行宽度更宽（120 vs 80，因为最终回答通常比思考长）
    - 默认直接 console.print 而非 [dim] 灰色（最终回答要高亮）
    """

    def __init__(self) -> None:
        self._buf: str = ""
        self._active: bool = False
        self._emitted_lines: int = 0
        self._target_width: int = 120

    def reset(self) -> None:
        """轮次开始时调用（TASK_START 触发）。"""
        self._buf = ""
        self._active = True
        self._emitted_lines = 0

    def feed(self, piece: str) -> Optional[str]:
        """累积一个增量；返回需要立即打印的一行（无则 None）。"""
        if not self._active or not piece:
            return None
        self._buf += piece
        if "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._emitted_lines += 1
                return line
        elif len(self._buf) >= self._target_width:
            cut = max(
                self._buf.rfind(b)
                for b in (" ", "\t", ".", "!", "?", ",", ";", ":")
            )
            if cut != -1 and cut >= self._target_width // 2:
                line, self._buf = self._buf[: cut + 1], self._buf[cut + 1 :]
                self._emitted_lines += 1
                return line
        return None

    def flush(self) -> Optional[str]:
        """STREAM_TEXT_END 时调用 — 刷出剩余 buffer。返回剩余一行（可能 None）。"""
        if not self._active:
            return None
        self._active = False
        tail = self._buf.strip()
        self._buf = ""
        if tail:
            self._emitted_lines += 1
            return tail
        return None


# 模块级单例：所有 STREAM_TEXT_DELTA / STREAM_TEXT_END 事件共享一个 buffer
_stream_text_box = _StreamTextBox()


# 模块级单例：所有 REASONING_DELTA 事件共享一个 buffer
_reasoning_stream = _ReasoningStream()


def _render_progress(event: Dict[str, Any]) -> None:
    """progress_notifier 推送的事件 sink：渲染到 CLI 终端。

    设计原则：
    - 单事件 = 一行，避免多行让用户压力
    - 使用 ASCII 标点 (─ · ✓ ✗) 避免 Windows GBK 终端乱码
    - 所有输出裹 try/except：sink 异常不影响主循环（progress_notifier 也隔离）
    - 不在 Live(Spinner) 里渲染（spinner 之外，由 chat() 控制 Live 生命周期）
    """
    try:
        phase = event.get("phase")
        summary = event.get("summary", "") or ""
        iterations = event.get("iterations", 0) or 0
        extra = event.get("extra") or {}

        # ── 字段读取双路径：优先顶层 (notify_progress._build_event 会把
        # extra 里的字段 merge 到 payload 顶层)，fallback 到 extra 嵌套 ──
        # 不这么写会读到 0 / None（之前版本 bug：调用工具显示 "0 个"、
        # 完成显示 "0.0s"）。
        def _get(key, default=None):
            v = event.get(key)
            if v is not None:
                return v
            return extra.get(key, default)

        duration_ms = _get("duration_ms")
        if phase == ProgressPhase.TASK_START.value:
            # v0.4.x：区分主代理 vs 子代理 task_start + 折叠多行 summary
            #
            # 背景：
            # - 每次 run_conversation() 入口都会发一次 task_start。
            # - 主代理 session_id = UUID（config.session_id 或 uuid.uuid4()）；
            #   子代理 session_id = f"child-{child_id}-a{attempt}"（delegate_tool._run_attempt）。
            # - 如果不区分，子代理的 task_start 会被视为“重复任务”迷惑用户，
            #   因为子代理的 user_message 模板是 "上下文:\n...\n\n任务: ..."，渲染后看起来和上下文块一模一样。
            # - 另外，summary 原本走 _truncate(s, 70) 截字符串，
            #   但 Rich console.print 会把 \n 渲染为换行，导致一个 task_start 被排成多行。
            #
            # 这里根据 session_id 前缀（"child-" 是子代理）区分标符，
            # 并把多行 summary 折叠成单行（首行 + (+N 行) 提示）。
            sid = _get("session_id", "") or ""
            is_child = sid.startswith("child-")
            folded = _fold_summary_single_line(summary, max_len=70)
            if is_child:
                console.print(
                    f"  [bold cyan]─── 👥 子任务开始[/bold cyan]  [dim]{folded}[/dim]"
                )
            else:
                console.print(
                    f"  [bold yellow]─── 任务开始[/bold yellow]  [dim]{folded}[/dim]"
                )
            # 新一轮：重置推理 stream + 流式回答 box，准备接收本轮 events
            _reasoning_stream.reset()
            _stream_text_box.reset()
        elif phase == ProgressPhase.THINKING.value:
            # 思考开始——不输出独立行（Live(Spinner) 正在显示），仅记录迭代
            _reasoning_stream.reset()
            pass
        elif phase == ProgressPhase.REASONING_DELTA.value:
            # 推理内容增量：累积后按自然边界打印（避免一词一行刷屏）
            # 由 config.display.show_reasoning 控制总开关（默认 True）
            try:
                _show_reasoning = True
                try:
                    from spirit.config import load_config
                    _cfg = load_config().get("display", {}) if isinstance(load_config(), dict) else {}
                    if isinstance(_cfg, dict) and "show_reasoning" in _cfg:
                        _show_reasoning = bool(_cfg["show_reasoning"])
                except Exception:
                    pass
                if _show_reasoning:
                    text = _get("text", "")
                    if text:
                        line = _reasoning_stream.feed(text)
                        if line is not None:
                            # v0.4.x：去掉 💭 emoji，box label " Reasoning " 已是标识
                            # 对齐 Hermes cli_stream_mixin._stream_reasoning_delta
                            console.print(f"  [dim]{line}[/dim]", highlight=False)
            except Exception as _rd_exc:
                # v0.4.x：避免完全静默 — 输出 dim yellow 一行（不阻塞主循环）
                # 调试时定位到 REASONING_DELTA 渲染失败但用户看不见的问题。
                try:
                    console.print(
                        f"  [dim yellow]⚠ reasoning render 异常: {_rd_exc}[/dim yellow]",
                        highlight=False,
                    )
                except Exception:
                    pass
        elif phase == ProgressPhase.REASONING_END.value:
            # 推理流结束：刷尾部剩余（如果有）
            try:
                _reasoning_stream.flush()
            except Exception:
                pass
        elif phase == ProgressPhase.THINKING_END.value:
            if duration_ms is not None:
                console.print(
                    f"  [dim]·[/dim] [yellow]推理完成[/yellow] [dim]({duration_ms / 1000:.1f}s，第 {iterations} 轮)[/dim]"
                )
        elif phase == ProgressPhase.TOOL_CALL.value:
            count = int(_get("count", 0) or 0)
            console.print(
                f"  [dim]·[/dim] [cyan]调用工具[/cyan] [bold]{summary}[/bold] [dim]({count} 个)[/dim]"
            )
        elif phase == ProgressPhase.TOOL_RESULT.value:
            ms = duration_ms if duration_ms is not None else 0
            count = int(_get("count", 0) or 0)
            # TOOL_RESULT 与 TOOL_CALL 的 summary 都是工具名拼接
            # count>0 时附加 "(N 个)" 与调用行保持格式一致
            count_tail = f"，{count} 个" if count > 0 else ""
            console.print(
                f"  [green]✓[/green] [dim]完成[/dim] [bold]{summary}[/bold]{count_tail} [dim]({ms / 1000:.1f}s)[/dim]"
            )
        # ── v0.4.x：新阶段分支（对齐 Hermes 工具生命周期 / 流式回答）─────
        elif phase == ProgressPhase.TOOL_GEN_START.value:
            # LLM 开始生成工具参数 — 显示 preparing spinner，让长参数生成期不"面观天亮"
            tool_name = _get("tool_name") or summary or "?"
            console.print(
                f"  [dim cyan]┊ ⚡ preparing {tool_name}…[/dim cyan]",
                highlight=False,
            )
        elif phase == ProgressPhase.TOOL_STARTED.value:
            # 工具启动 — 显示工具名 + 参数预览（受 show_tool_args 控制）
            name = _get("tool_name") or summary or "?"
            args = _get("function_args") or {}
            display_args = _get("display_args") or args
            preview = _get("preview") or _format_tool_args(display_args)
            try:
                _show_args = True
                from spirit.config import load_config as _lc
                _dc = _lc().get("display", {})
                if isinstance(_dc, dict) and "show_tool_args" in _dc:
                    _show_args = bool(_dc["show_tool_args"])
            except Exception:
                _show_args = True
            if _show_args and preview:
                console.print(
                    f"  [dim cyan]┊ {name}[/dim cyan]([yellow]{preview}[/yellow])",
                    highlight=False,
                )
            else:
                console.print(
                    f"  [dim cyan]┊ {name}[/dim cyan]()",
                    highlight=False,
                )
        elif phase == ProgressPhase.TOOL_COMPLETED.value:
            # 工具完成 — 单行 cute 消息（受 show_tool_result 控制）
            name = _get("tool_name") or summary or "?"
            duration = (_get("duration_ms") or 0) / 1000.0
            is_error = bool(_get("is_error"))
            result = _get("result", "")
            try:
                _show_result = True
                _max_len = 300
                from spirit.config import load_config as _lc
                _dc = _lc().get("display", {})
                if isinstance(_dc, dict):
                    if "show_tool_result" in _dc:
                        _show_result = bool(_dc["show_tool_result"])
                    if "tool_result_max_len" in _dc:
                        _max_len = int(_dc["tool_result_max_len"])
            except Exception:
                _show_result = True
                _max_len = 300
            icon = "✗" if is_error else "✓"
            color = "red" if is_error else "green"
            duration_ms = int((_get("duration_ms") or 0))
            if _show_result:
                cute = _format_cute_tool_message(
                    name=name,
                    args=_get("display_args") or _get("function_args"),
                    duration_ms=duration_ms,
                    result=result,
                    is_error=is_error,
                )
                console.print(f"  [{color}]{cute}[/{color}]", highlight=False)
            else:
                # 仅显示耗时行（关闭 show_tool_result 时）
                console.print(
                    f"  [{color}]{icon}[/{color}] [bold]{name}[/bold] "
                    f"[dim]({duration:.1f}s)[/dim]",
                    highlight=False,
                )
        elif phase == ProgressPhase.STREAM_TEXT_DELTA.value:
            # 最终回答流式 token — 按自然边界 flush（受 show_stream_text 控制）
            # v0.4.x：响应到达时协同关闭 reasoning box（对齐 Hermes _close_reasoning_box）
            try:
                _reasoning_stream.close_box()
            except Exception:
                pass
            try:
                _show_stream = True
                from spirit.config import load_config as _lc
                _dc = _lc().get("display", {})
                if isinstance(_dc, dict) and "show_stream_text" in _dc:
                    _show_stream = bool(_dc["show_stream_text"])
            except Exception:
                _show_stream = True
            if _show_stream:
                text = _get("text", "")
                if text:
                    line = _stream_text_box.feed(text)
                    if line is not None:
                        console.print(f"  {line}", highlight=False)
        elif phase == ProgressPhase.STREAM_TEXT_END.value:
            # 流式结束 — 刷出尾部剩余
            try:
                tail = _stream_text_box.flush()
                if tail:
                    console.print(f"  {tail}", highlight=False)
            except Exception:
                pass
        elif phase == ProgressPhase.THINKING_AVAILABLE.value:
            # 整段思考文本落地（非流式）— 按行折叠
            thinking_text = _get("thinking_text", "")
            if thinking_text:
                lines = thinking_text.split("\n")
                for line in lines[:5]:
                    if line.strip():
                        console.print(
                            f"  [dim]💭 {line.strip()}[/dim]",
                            highlight=False,
                        )
                if len(lines) > 5:
                    console.print(
                        f"  [dim]… (+{len(lines) - 5} more lines)[/dim]",
                        highlight=False,
                    )
        elif phase == ProgressPhase.TASK_COMPLETE.value:
            reason = _get("reason", "")
            tail = f"  [dim]({reason})[/dim]" if reason else ""
            console.print(
                f"  [bold green]─── 任务完成[/bold green]  [dim]({iterations} 轮)[/dim]{tail}"
            )
        elif phase == ProgressPhase.TASK_ABORTED.value:
            reason = _get("reason", "unknown")
            console.print(
                f"  [bold red]─── 任务中断[/bold red]  [dim]({reason})[/dim]"
            )
    except Exception:
        # sink 异常隔离：不影响主循环（与 progress_notifier 内部逻辑一致）
        pass


FOX_LOGO = r"""
    /\_   _/\
   ( o . o )
    >  v  <
   /|     |\
  (_|     |_)
"""


def _print_welcome_panel(agent, tool_count: int) -> None:
    """顶部黄色欢迎面板 — 命令提示（无 ASCII 狐狸，避免与外层 TUI wrapper 重复显示）。

    v0.4.x 改动：去掉 FOX_LOGO + body 里的 "Spirit Agent CLI v1.0" 标题（Panel title
    已经是 "Spirit Agent CLI"），避免与外层 TUI 启动器（Spirit Agent + SPIRIT 大字）
    在终端重复显示两次图标。命令提示保持不变。
    """
    body = Text()
    body.append("输入 ", style="white")
    body.append("/help", style="bold yellow")
    body.append(" 查看可用命令\n", style="white")
    body.append("直接输入文字与 ", style="white")
    body.append("Agent", style="bold yellow")
    body.append(" 对话", style="white")

    console.print(Panel(
        body,
        title="[bold yellow]Spirit Agent CLI[/bold yellow]",
        title_align="left",
        border_style="yellow",
        box=__import__("rich").box.HEAVY,
        padding=(0, 2),
    ))


def _print_connection_status() -> None:
    """已连接状态行。"""
    console.print("[green]√[/green] 已连接到 Spirit Agent 后端")


def _print_recent_tasks(db, limit: int = 5) -> None:
    """列出最近 N 个会话（[n] 编号 + 首条用户消息预览）。

    用户输入 [n] / resume <n> 时会调用 agent.resume_session(session_id)。

    v0.3.0 (Hermes-style): 优先用 sessions.title 字段（auto/user），没有
    title 才 fallback 到首条 user 消息。解决「空会话」问题 + 避免重复拼的预览闪。
    """
    if db is None:
        return
    try:
        sessions = db.list_sessions(limit=limit)
    except Exception:
        sessions = []
    if not sessions:
        return

    console.print(RECENT_HEADER)
    for idx, s in enumerate(sessions, start=1):
        preview = ""
        # v0.3.0: 优先读 sessions.title（auto 或 user）
        title = (s.get("title") or "").strip()
        if title:
            preview = title
        else:
            # Fallback: 首条用户消息
            try:
                msgs = db.get_messages(s["id"])
            except Exception:
                msgs = []
            for m in msgs:
                if m.get("role") == "user" and m.get("content"):
                    preview = m["content"].replace("\n", " ").strip()
                    break
            if not preview:
                preview = "(空会话)"
        if len(preview) > 50:
            preview = preview[:50] + "..."
        started = _fmt_local_ts(s.get("started_at", ""))  # v0.3.4: UTC → 本地

        line = Text()
        line.append(f"  [{idx}] ", style="yellow")
        line.append(preview + "  ", style="white")
        line.append(f"[{started}]", style="dim")
        console.print(line)


def _resolve_resume_index(db, user_input: str, limit: int = 5) -> Optional[str]:
    """如果用户输入是 [n] 或 resume <n>，返回对应 session_id。

    返回 None 表示不是恢复命令。返回非 None 表示应当 resume。
    """
    text = user_input.strip()
    if db is None:
        return None
    # [n]
    if text.startswith("[") and text.endswith("]"):
        try:
            idx = int(text[1:-1])
        except ValueError:
            return None
    elif text.lower().startswith("resume "):
        try:
            idx = int(text.split(maxsplit=1)[1])
        except (ValueError, IndexError):
            return None
    else:
        return None
    sessions = db.list_sessions(limit=limit)
    if 1 <= idx <= len(sessions):
        return sessions[idx - 1]["id"]
    return "__NOT_FOUND__"


def setup_logging(verbose: bool = False):
    """配置日志 — 默认 ERROR（仅错误），verbose 时开 INFO（看 LLM 链路）。

    设计原则：用户不需要看 WARNING/INFO 噪声。任务里需要的关键 WARNING
    （如 LLM 思考中的提示）由 progress_notifier 推送到可视化层；全量日志
    仅 verbose 模式开。
    """
    level = logging.DEBUG if verbose else logging.ERROR
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    # spirit.* 命名空间 logger 兜底（防止子模块继承 root 后仍输出 INFO）
    logging.getLogger("spirit").setLevel(level)
    # 库级 logger 也不在终端暴露（urllib3 / httpcore / openai 等）
    for noisy in ("urllib3", "httpcore", "httpx", "openai", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


@click.group(invoke_without_command=True)
@click.option("-v", "--verbose", is_flag=True, help="详细日志")
@click.pass_context
def cli(ctx, verbose):
    """Spirit Agent — 统一后端 AI Agent 服务

    \b
    不带子命令直接调用 ``spirit`` 会默认进入交互式对话（与 Windows 行为一致）。
    其他子命令（start / sessions / tools / storage ...）仍可显式指定。
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    setup_logging(verbose)

    # 默认行为: ``spirit`` 无子命令 → 自动进入对话模式
    # （跨平台一致：Windows / macOS / Linux 直接 ``spirit`` 都进入 chat）
    if ctx.invoked_subcommand is None:
        # chat 子命令的可选参数都有 default=None，透传即可
        ctx.invoke(chat, model=None, api_key=None, base_url=None)


@cli.command()
@click.option("--model", "-m", default=None, help="模型名称")
@click.option("--api-key", envvar="SPIRIT_API_KEY", help="API Key")
@click.option("--base-url", envvar="SPIRIT_BASE_URL", help="API Base URL")
def chat(model, api_key, base_url):
    """开始交互式对话"""
    from spirit.agent.agent import AgentConfig, SpiritAgent

    # 构建配置 — 统一走集中式配置系统
    from spirit.config import load_config
    raw = load_config()
    llm_cfg = raw.get("llm", {})

    config = AgentConfig(
        model=model or llm_cfg.get("model", ""),
        api_key=api_key or llm_cfg.get("api_key", ""),
        base_url=base_url or llm_cfg.get("base_url", ""),
    )

    if not config.api_key:
        console.print("[red]错误：未设置 API Key。请设置 SPIRIT_API_KEY 环境变量或使用 --api-key 参数。[/red]")
        sys.exit(1)

    # 创建 Agent
    agent = SpiritAgent(config)

    # 注册交互式危险命令审批回调 + 标记交互式会话
    from spirit.tools.approval import set_approval_callback, set_interactive_context

    def _approval_callback(command: str, description: str, pattern_key: str) -> str:
        console.print(Panel.fit(
            f"[red]{command}[/red]\n\n原因: [cyan]{description}[/cyan]",
            title="[bold yellow]⚠ 危险命令需审批[/bold yellow]",
            border_style="yellow",
        ))
        try:
            ans = console.input("允许执行? y=本次 / a=总是 / n=拒绝（默认 n）: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("a", "always"):
            return "always"
        if ans in ("y", "yes", "session"):
            return "session"
        return "deny"

    set_approval_callback(_approval_callback)
    set_interactive_context(True)

    # --- 内部回调接线 ---
    # 移除 _silent no-op：现在阶段输出走 progress_notifier 7 阶段事件总线
    # （conversation_loop 在 9 个点调 notify_progress）。这样 LLM 推理中的
    # 工具调用 / 思考耗时 / 任务完成都能在终端看到。
    # 不设 agent.on_tool_start/complete/stream_delta（保持默认 None）——
    # conversation_loop 不走这 3 个回调，而是全部走 progress_notifier。
    register_progress_sink(_render_progress)

    # 会话数据库（用于历史任务列表）
    db = getattr(agent, "_session_db", None)

    # 顶部欢迎面板 + 连接状态 + 最近任务
    _print_welcome_panel(agent, tool_count=len(agent.get_tool_definitions()))
    _print_connection_status()
    _print_recent_tasks(db, limit=5)
    console.print()  # 空行分隔输入

    # 底部状态栏 + 黄色 prompt
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText, HTML
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.styles import Style

        # 状态栏样式字典（与 STATUS_STYLE 映射一致）
        # prompt_toolkit 的 HTML() 不解析 Rich markup，所以底部状态栏必须用
        # FormattedText 片段，每个片段是 (style_class, text) 元组。
        prompt_style = Style.from_dict({
            "prompt": "ansiyellow bold",
            "indicator": "ansiyellow",
            # 状态栏专用样式
            "status-bar":        "ansiyellow",
            "status-bar-dim":    "ansigray",
            "status-bar-strong": "ansiyellow bold",
            "status-bar-warn":   "ansiyellow",
            "status-bar-bad":    "ansired bold",
            "status-bar-good":   "ansigreen",
            "status-bar-thinking": "ansiyellow bold",
            "status-bar-tool":   "ansicyan",
        })

        # 会话状态容器
        class _State:
            thinking: bool = False
            elapsed: float = 0.0
            last_tools: List[str] = []  # 最近一轮调用的工具名拼接
            iterations: int = 0        # 最近一轮的 API 调用次数

        state = _State()

        def _bottom_toolbar():
            """用 FormattedText 拼状态栏片段，不再用 HTML() 包装 Rich markup。

            v0.2.10 统一风格 + ctx 标签 + since 时间戳：
              - 字段顺序：状态 | 模型 | ctx [bar] 15% 19K/128K | since HH:MM:SS
                          | Σ 45K ⇄ 3 calls | 28s | tools: web_search( | #msgs | Ttools
              - 全部 info 段统一前景色 (status-bar 黄)，只让 ctx 进度条走阈值色。
              - `ctx` 标签让用户明确「这是上下文用量」而不是「官网 quota」。
              - `since HH:MM:SS` 是会话启动时间——给「累计从何时起」一个明确锚点。
                （未来可接 AccountUsageSnapshot 后补 quota / reset_at。）
              - 累计字段精简：去掉 in/out 拆开（占位太重）→ `Σ 45K ⇄ 3 calls`。
              - 统一分隔符 ` | `（一个空格 + | + 一个空格）——与 hermes _status_bar_segments 对齐。
            """
            # ── 状态标签 ──
            if state.thinking:
                head = [("class:status-bar-thinking", "思考中")]
            else:
                head = [("class:status-bar-strong", "空闲")]

            # ── 模型名 ──
            model_txt = (agent.model or "?").strip()
            if len(model_txt) > 26:
                model_txt = model_txt[:23] + "..."

            # ── 耗时（仅当前轮，spinner 期间递增）──
            elapsed_txt = _fmt_duration(state.elapsed)

            # ── 工具列表 ──
            tool_summary = state.last_tools or ""
            if len(tool_summary) > 24:
                tool_summary = tool_summary[:21] + "..."

            # ── msgs / tools 计数 ──
            msgs = len(agent.messages)
            tools = len(agent.get_tool_definitions())

            # ── since HH:MM:SS 会话启动时间（v0.2.10）──
            # 「更新时间错」问题解答：spirit 状态栏里每一个计数（Σ、ctx）
            # 都以 _session_started_at 为起点——所以标在状态栏上更透明。
            # 进程重启后 _session_started_at 丢失，下一行为从 db.started_at 补。
            since_txt = None
            try:
                _started = getattr(agent, "_session_started_at", None)
                if _started is None and getattr(agent, "_session_db", None) and agent.session_id:
                    _row = agent._session_db.get_session(agent.session_id) or {}
                    _iso = _row.get("started_at")
                    if _iso:
                        try:
                            # v0.3.4 fix: DB 已改为本地时间，timestamp() 直接转换
                            _started = datetime.fromisoformat(str(_iso)).timestamp()
                        except Exception:
                            _started = None
                if _started:
                    from datetime import datetime
                    since_txt = datetime.fromtimestamp(float(_started)).strftime("%H:%M:%S")
            except Exception:
                since_txt = None

            # ── 上下文 ctx% / 详情（加 ctx 标签，v0.2.10）──
            # 进度条 + 百分比 + used/total，样式对齐 hermes _build_context_bar。
            ctx_frag = None
            try:
                compressor = getattr(agent, "_context_compressor", None)
                if compressor:
                    ctx_len = int(getattr(compressor, "context_length", 0) or 0)
                    # 优先拿 _last_prompt_tokens；fallback 拿公开属性
                    tokens = int(
                        getattr(compressor, "_last_prompt_tokens", 0)
                        or getattr(compressor, "last_prompt_tokens", 0)
                        or 0
                    )
                    if ctx_len > 0 and tokens >= 0:
                        pct = round((tokens / ctx_len) * 100)
                        pct = max(0, min(100, pct))
                        # 阈值色：绿 ≤50 / 黄 50-80 / 红 >80（仅进度条用）
                        if pct > 80:
                            ctx_class = "class:status-bar-bad"
                        elif pct > 50:
                            ctx_class = "class:status-bar-warn"
                        else:
                            ctx_class = "class:status-bar-good"
                        # 进度条（10 格宽度，和 hermes _build_context_bar 一致）
                        bar_width = 10
                        filled = round((pct / 100) * bar_width)
                        empty = max(0, bar_width - filled)
                        bar = f"[{chr(0x2588) * filled}{chr(0x2591) * empty}]"
                        # ctx 标签 + 进度条 + 百分比 + used/total
                        # 「ctx」明确这是上下文用量（不是官网 quota）
                        ctx_frag = [
                            ("class:status-bar-dim", " | "),
                            ("class:status-bar", "ctx "),
                            (ctx_class, bar),
                            ("class:status-bar", f" {pct}% {_fmt_tokens(tokens)}/{_fmt_tokens(ctx_len)}"),
                        ]
            except Exception:
                ctx_frag = None

            # ── Σ 会话用量：in + out + API calls（精简版）──
            try:
                in_tok = int(getattr(agent, "session_input_tokens", 0) or 0)
                out_tok = int(getattr(agent, "session_output_tokens", 0) or 0)
                total_tok = int(getattr(agent, "session_total_tokens", 0) or 0)
                api_calls = int(getattr(agent, "session_api_calls", 0) or 0)
            except Exception:
                in_tok = out_tok = total_tok = api_calls = 0

            sum_frag = None
            if total_tok > 0 or api_calls > 0:
                # 精简为 `Σ 45K ⇄ 3 calls`（去括号、去掉 in/out 拆开）
                sum_frag = [
                    ("class:status-bar-dim", " | "),
                    ("class:status-bar", f"Σ {_fmt_tokens(total_tok)} ⇄ {api_calls} calls"),
                ]

            # ── 拼装全部片段（统一前景色 status-bar 黄）──
            frags = [
                ("class:status-bar-dim", "  "),
                *head,
                ("class:status-bar-dim", " | "),
                ("class:status-bar", model_txt),
            ]
            if ctx_frag:
                frags.extend(ctx_frag)
            if since_txt:
                frags.extend([
                    ("class:status-bar-dim", " | "),
                    ("class:status-bar", f"since {since_txt}"),
                ])
            if sum_frag:
                frags.extend(sum_frag)
            # 剩余字段：耗时 / 工具列表 / msgs / tools
            frags.extend([
                ("class:status-bar-dim", " | "),
                ("class:status-bar", elapsed_txt),
                ("class:status-bar-dim", " | "),
                ("class:status-bar", "tools: "),
                ("class:status-bar", tool_summary or "-"),
                ("class:status-bar-dim", " | "),
                ("class:status-bar", f"#{msgs}"),
                ("class:status-bar-dim", " | "),
                ("class:status-bar", f"T{tools}"),
            ])
            return FormattedText(frags)

        session = PromptSession(
            history=InMemoryHistory(),
            bottom_toolbar=_bottom_toolbar,
            style=prompt_style,
            message=HTML("<prompt>spirit</prompt> <indicator>></indicator> "),
        )
    except (ImportError, Exception):
        # ImportError: prompt_toolkit 未安装
        # OSError/NoConsoleScreenBufferError: stdio 被重定向（CI、管道）无 Win32 console
        session = None
        state = type("_S", (), {"thinking": False, "elapsed": 0.0, "last_tools": [], "iterations": 0})()

    import threading

    while True:
        try:
            if session:
                user_input = session.prompt()
            else:
                user_input = input("spirit > ")

            user_input = user_input.strip()
            if not user_input:
                continue

            # --- 恢复历史任务 [n] / resume <n> ---
            target = _resolve_resume_index(db, user_input)
            if target == "__NOT_FOUND__":
                console.print("[red]编号超出范围[/red]")
                continue
            if target:
                if agent.resume_session(target):
                    console.print(f"[green]已恢复会话 {target[:8]}[/green]")
                else:
                    console.print("[red]恢复失败[/red]")
                continue

            # --- /tasks 重显最近任务 ---
            if user_input.lower() in ("/tasks", "/history"):
                _print_recent_tasks(db, limit=5)
                continue

            # --- 其他斜杠命令 ---
            if user_input.startswith("/"):
                if _handle_slash_command(user_input, agent):
                    continue
                else:
                    break

            # --- 对话：Spinner 表示思考中，阶段输出在 spinner 结束后由 progress_sink 渲染 ---
            state.thinking = True
            state.last_tools = ""
            state.iterations = 0
            t0 = time.time()
            spinner_text = Text()
            spinner_text.append("spirit 正在思考", style="bold yellow")
            spinner_text.append("  ·  ", style="dim")
            spinner_text.append(agent.model or "?", style="yellow")

            # 启动计时线程以刷新底部 elapsed
            stop_tick = threading.Event()

            def _tick():
                while not stop_tick.is_set():
                    state.elapsed = time.time() - t0
                    time.sleep(0.5)

            tick_thread = threading.Thread(target=_tick, daemon=True)
            tick_thread.start()

            # spinner 期间临时屏蔽 logger（防止内部 INFO 漏到 spinner 之外的 stdout）
            # CRITICAL > ERROR：连 WARNING 也不输出，确保 spinner 期间干净
            _root_logger = logging.getLogger()
            _prev_level = _root_logger.level
            _root_logger.setLevel(logging.CRITICAL)
            try:
                with Live(
                    Spinner("dots", text=spinner_text),
                    console=console,
                    transient=True,
                    refresh_per_second=8,
                ):
                    result = agent.run_conversation(user_input)
            finally:
                stop_tick.set()
                state.thinking = False
                state.elapsed = time.time() - t0
                # 恢复原 logger level（保持 verbose 模式仍能看后续警告）
                _root_logger.setLevel(_prev_level)

            # --- 阶段输出已在 spinner 过程中由 progress_sink 渲染；现在汇总本轮结果 ---
            state.iterations = getattr(result, "iterations", 0) or 0
            tool_calls = getattr(result, "tool_calls", None) or []

            # 状态栏展示最近一轮调用的工具名拼接（供底部 toolbar 轮末更新）
            if tool_calls:
                state.last_tools = _format_tool_call_summary(tool_calls)

            # --- 最终响应：阶段标题 + Markdown 渲染 ---
            if result.response:
                # 工具调用摘要：fallback——如果 progress_sink 没接上，工具调用会显示
                if tool_calls and not getattr(_render_progress, "_sinked", False):
                    console.print(
                        f"  [dim]本轮调用了 {len(tool_calls)} 个工具：[/dim]"
                        f"  [cyan]{_format_tool_call_summary(tool_calls)}[/cyan]"
                    )
                # console.print("[bold yellow]spirit>[/bold yellow]")
                # 响应体用 Markdown 渲染（与原版一致）
                console.print(Markdown(result.response))
                console.print()  # 空行分隔
            else:
                console.print("[dim](空响应)[/dim]")

        except KeyboardInterrupt:
            console.print("\n[dim]按 Ctrl+C 再次退出，或输入 /quit[/dim]")
            continue
        except EOFError:
            break

    # 结束前反注册 progress_sink（避免多 CLI 实例之间的泄漏）
    try:
        unregister_progress_sink(_render_progress)
    except Exception:
        pass
    # ── v0.2.9: CLI 退出时调 close_session 兑底（幂等）──
    # run_conversation finally 本身会调一次 (成功/异常路径已覆盖)，
    # 这里仅覆盖「用户 /quit 提前退出」场景——中间轮就被打断，
    # 该轮 task 标记为 cli_exit，与 session_persistence 三层终止对齐。
    try:
        agent.close_session(reason="cli_exit")
    except Exception:  # noqa: BLE001
        pass
    console.print("\n[dim]再见！[/dim]")


def _handle_slash_command(cmd: str, agent) -> bool:
    """处理斜杠命令。返回 True 表示继续，False 表示退出。"""
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command in ("/quit", "/exit", "/q"):
        return False

    elif command == "/help":
        console.print(Panel(
            "[bold]命令列表：[/bold]\n"
            "  /help        显示帮助\n"
            "  /new         新建会话\n"
            "  /model       切换模型\n"
            "  /status      显示状态\n"
            "  /tools       列出工具\n"
            "  /sessions    会话历史\n"
            "  /pet         启动桌面小狐狸\n"
            "  /quit        退出",
            title="帮助",
            border_style="blue",
        ))

    elif command == "/new":
        agent.reset_session()
        console.print("[green]新会话已创建[/green]")

    elif command == "/model":
        if args:
            agent.switch_model(args)
            console.print(f"[green]模型已切换: {args}[/green]")
        else:
            console.print(f"当前模型: [cyan]{agent.model}[/cyan]")

    elif command == "/status":
        status = agent.get_status()
        lines = [f"  {k}: [cyan]{v}[/cyan]" for k, v in status.items()]
        console.print(Panel("\n".join(lines), title="状态", border_style="blue"))

    elif command == "/tools":
        tools = agent.get_tool_definitions()
        entries = agent.registry.get_all_entries()
        for entry in entries:
            console.print(f"  {entry.emoji} [bold]{entry.name}[/bold] [dim]({entry.toolset})[/dim]")
            if entry.description:
                desc = entry.description[:80] + "..." if len(entry.description) > 80 else entry.description
                console.print(f"    [dim]{desc}[/dim]")

    elif command == "/sessions":
        try:
            from spirit.storage.session_db import SessionDB
            db = SessionDB()
            sessions = db.list_sessions(limit=10)
            if sessions:
                for s in sessions:
                    console.print(
                        f"  [cyan]{s['id'][:8]}[/cyan] "
                        f"{s['source']} | {s['model']} | {_fmt_local_ts(s['started_at'])}"  # v0.3.4: UTC → 本地
                    )
            else:
                console.print("  [dim]暂无会话记录[/dim]")
            db.close()
        except Exception as e:
            console.print(f"  [red]查询失败: {e}[/red]")

    elif command == "/pet":
        # 启动/唤起桌面小狐狸。委托给 ``spirit pet`` 子命令（v0.3.3 修复）。
        # 原 v0.3.2 逻辑是 “if 端口已听 → 提示；else → spawn spirit pet （无此子命令），
        # 被 DEVNULL 吞掉 click 报错”，造成“提示已启动但什么都不发生”的假象。
        import subprocess
        import shutil
        import platform
    
        is_windows = platform.system() == "Windows"
        spirit_cmd = "spirit.cmd" if is_windows else "spirit"
        spirit_path = (
            shutil.which(spirit_cmd)
            or shutil.which("spirit")
            or (r"C:\Users\Administrator\bin\spirit.cmd" if is_windows else None)
        )
        if spirit_path is None:
            console.print(Panel(
                "[bold red]找不到 'spirit' 命令[/bold red]\n"
                "[dim]请确认 spirit 已安装且在 PATH 中[/dim]",
                border_style="red",
            ))
            return True
        try:
            # sync 走 pet 子命令，等看到端口起来再回来
            subprocess.run([spirit_path, "pet", "--wait"], check=False)
        except FileNotFoundError:
            console.print(Panel(
                f"[bold red]执行 spirit pet 失败[/bold red]\n[dim]尝试了路径: {spirit_path}[/dim]",
                border_style="red",
            ))

    else:
        console.print(f"[red]未知命令: {command}[/red] 输入 /help 查看帮助")

    return True


@cli.command()
@click.option("--host", default="127.0.0.1", help="监听地址")
@click.option("--port", "-p", default=8765, help="监听端口")
@click.option("--reload", is_flag=True, help="开发模式（自动重载）")
def start(host, port, reload):
    """启动后端 API 服务（REST + WebSocket）"""
    console.print(Panel.fit(
        f"[bold green]Spirit Agent Server[/bold green] v0.1.0\n"
        f"地址: [cyan]http://{host}:{port}[/cyan]\n"
        f"WebSocket: [cyan]ws://{host}:{port}/ws/chat[/cyan]\n"
        f"API 文档: [cyan]http://{host}:{port}/docs[/cyan]",
        border_style="green",
    ))
    from spirit.api.server import run_server
    run_server(host=host, port=port, reload=reload)


@cli.command()
def sessions():
    """查看会话历史"""
    try:
        from spirit.storage.session_db import SessionDB
        db = SessionDB()
        session_list = db.list_sessions(limit=20)
        if session_list:
            for s in session_list:
                console.print(
                    f"[cyan]{s['id'][:8]}[/cyan] | "
                    f"{s['source']:10} | "
                    f"{s['model']:20} | "
                    f"{_fmt_local_ts(s['started_at'])}"  # v0.3.4: UTC → 本地
                )
        else:
            console.print("[dim]暂无会话记录[/dim]")
        db.close()
    except Exception as e:
        console.print(f"[red]错误: {e}[/red]")


@cli.command()
def tools():
    """列出可用工具"""
    from spirit.tools.registry import registry, discover_tools
    discover_tools()
    entries = registry.get_all_entries()
    if entries:
        for entry in entries:
            console.print(f"{entry.emoji} [bold]{entry.name}[/bold] [dim]({entry.toolset})[/dim]")
    else:
        console.print("[dim]暂无已注册工具[/dim]")


# ---------------------------------------------------------------------------
# v0.3.3 pet 子命令：启动 Electron 桌宠（v0.3.2 只是 CLI 提示，桌宠本身未启动）
# ---------------------------------------------------------------------------

def _resolve_spirit_project_root() -> Optional[Path]:
    """定位 spirit_agent 项目根（与 spirit/desktop/_launcher.py 的算法一致）。

    算法：__file__ 向上跳 3 层（.../spirit/cli/main.py -> .../spirit/cli -> .../spirit -> .../spirit_agent）。
    失败时返回 None（安装后可能被移到 site-packages，根不再可用）。
    """
    try:
        here = Path(__file__).resolve()
        # .../spirit_agent/spirit/cli/main.py
        root = here.parent.parent.parent
        if (root / "desktop-app" / "dist-electron" / "main.js").exists():
            return root
    except Exception:
        pass
    return None


@cli.command()
@click.option("--wait/--no-wait", default=True,
              help="是否等待桌宠 WebSocket 端口启动后退出（默认等 10s）")
def pet(wait: bool):
    """启动桌面小狐狸（Electron 桌宠 + Python WebSocket 后端）。

    与 ``/pet`` 斜杠命令的差异：本子命令是桌宠的真正启动入口，
    它会 spawn Electron 二进制，Electron 主进程会负责启动 Python WebSocket 后端。
    如果桌宠已经在跑，提示用户复用现有窗口。
    """
    import subprocess
    import platform

    is_windows = platform.system() == "Windows"
    ws_port = int(os.environ.get("SPIRIT_WS_PORT", "9877"))

    # 1. 端口已在听 -> 桌宠应该已起，提示用户复用 / 重启
    try:
        if is_windows:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -State Listen -LocalPort {ws_port} "
                 "-ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty OwningProcess"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            running_pid = r.stdout.strip()
        else:
            r = subprocess.run(
                ["lsof", "-ti", f":{ws_port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            running_pid = r.stdout.strip()
    except Exception:
        running_pid = ""

    # v0.3.6: 额外检测 Electron 渲染进程。之前只看 launcher 端口，
    # 一旦 Electron GUI 死了但 launcher 还在，命令会误判 "已运行" 什么都不做
    # （用户会看到“桌宠已在运行”但实际窗口没了）。
    electron_pids = _find_electron_main_pids()

    if running_pid and electron_pids:
        # 状态 B: launcher + Electron 都健康 -> 提示已运行
        if is_windows:
            msg = (
                f"[bold yellow]桌宠已在运行[/bold yellow]（后端 PID [cyan]{running_pid}[/cyan]，"
                f"Electron PID [cyan]{','.join(electron_pids)}[/cyan]）\n\n"
                "[dim]看不到桌宠？右键系统托盘 Spirit 图标 → 显 示/隐藏[/dim]\n"
                "[dim]仍看不到？托盘图标可能在「隐藏的图标」里，点右下小箭头展开[/dim]"
            )
        else:
            msg = (
                f"[bold yellow]桌宠已在运行[/bold yellow]（后端 PID [cyan]{running_pid}[/cyan]，"
                f"Electron PID [cyan]{','.join(electron_pids)}[/cyan]）\n\n"
                "[dim]看不到桌宠？检查 Dock 或切换窗口（Cmd+Tab）[/dim]\n"
                "[dim]仍看不到？托盘菜单 → 重置位置 ，可把桌宠拉到右下角可见位置[/dim]"
            )
        console.print(Panel(msg, border_style="yellow"))
        return

    if running_pid and not electron_pids:
        # 状态 C: launcher 在但 Electron 死了 -> 自动只重启 Electron（保留后端）
        console.print(Panel(
            f"[bold yellow]检测到后端 launcher 在跑但 Electron GUI 已退出[/bold yellow]\n"
            f"（后端 PID [cyan]{running_pid}[/cyan]）\n\n"
            "[dim]正在自动重启桌宠窗口（保留后端进程）…[/dim]",
            border_style="yellow"
        ))
        _spawn_electron_only(wait=wait, ws_port=ws_port, is_windows=is_windows)
        return

    if not running_pid and electron_pids:
        # 状态 D: 罕见 — Electron 在但 launcher 没起（崩溃中）。杀 electron 走完整重启。
        console.print(Panel(
            f"[bold yellow]检测到 Electron 渲染进程在跑但后端 9877 未起[/bold yellow]\n"
            f"（Electron PID [cyan]{','.join(electron_pids)}[/cyan]）\n\n"
            "[dim]正在清理并完整重启桌宠…[/dim]",
            border_style="yellow"
        ))
        for pid in electron_pids:
            try:
                os.kill(int(pid), 15)  # SIGTERM
            except Exception:
                pass
        # 走下面的状态 A 完整启动
    else:
        # 状态 A: 都没起 -> 完整启动
        pass

    # 2. 端口未听 -> 查 project root 与 electron 二进制
    project_root = _resolve_spirit_project_root()
    if project_root is None:
        console.print(Panel(
            "[bold red]找不到 spirit_agent 项目根[/bold red]\n"
            "[dim]只支持从源码仓库运行 spirit pet；wheel/pip 安装后请手动启动 desktop-app。[/dim]",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)

    electron_bin = project_root / "desktop-app" / "node_modules" / ".bin" / "electron"
    electron_main = project_root / "desktop-app" / "dist-electron" / "main.js"
    if not electron_bin.exists():
        console.print(Panel(
            f"[bold red]未找到 electron 二进制[/bold red]\n[dim]{electron_bin}[/dim]\n\n"
            "[dim]请在 desktop-app/ 目录运行：[/dim]\n"
            "[dim]  npm install[/dim]\n"
            "[dim]  npm run build:vite[/dim]",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)
    if not electron_main.exists():
        console.print(Panel(
            f"[bold red]未找到 dist-electron/main.js[/bold red]\n[dim]{electron_main}[/dim]\n\n"
            "[dim]请在 desktop-app/ 目录运行：npm run build:vite[/dim]",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)

    # 3. spawn Electron。cwd = spirit_agent 根（与手动启动一致）
    _spawn_electron_and_wait(wait=wait, ws_port=ws_port, is_windows=is_windows,
                              project_root=project_root,
                              electron_bin=electron_bin, electron_main=electron_main)


def _find_electron_main_pids() -> list:
    """检测桌宠 Electron 主进程是否还活着。

    返回所有包含 ``desktop-app/dist-electron/main.js`` 命令行的 PID 列表。
    macOS / Linux 通用（用 ``ps -eo pid,command``）。
    Windows 暂未实现（返回空，靠 launcher 端口检测兜底）。
    """
    import subprocess
    import platform
    if platform.system() == "Windows":
        return []
    try:
        r = subprocess.run(  # noqa: PLW1509 — local import in function
            ["ps", "-eo", "pid,command"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        pids = []
        for line in r.stdout.splitlines():
            if "dist-electron/main.js" in line and "grep" not in line:
                parts = line.split(None, 1)
                if parts and parts[0].isdigit():
                    pids.append(parts[0])
        return pids
    except Exception:
        return []


def _spawn_electron_and_wait(
    wait: bool,
    ws_port: int,
    is_windows: bool,
    project_root: Any,
    electron_bin: Any,
    electron_main: Any,
) -> None:
    """实际 spawn Electron + 等待 ws 端口起来的 helper。"""
    import subprocess
    import time as _time
    console.print(f"[cyan]正在启动桌宠…[/cyan] electron = {electron_bin}")
    try:
        popen_kwargs = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        if is_windows:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [str(electron_bin), str(electron_main)],
            cwd=str(project_root),
            **popen_kwargs,
        )
    except FileNotFoundError as e:
        console.print(Panel(
            f"[bold red]spawn electron 失败[/bold red]\n[dim]{e}[/dim]",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)

    if not wait:
        console.print(Panel(
            "[bold green]桌宠已在后台启动[/bold green]\n"
            "[dim]WebSocket 端口稍后会起于 9877。[/dim]",
            border_style="green",
        ))
        return

    # 4. 等待 9877 端口起来（超时 10s）
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        _time.sleep(0.5)
        try:
            r = subprocess.run(
                ["lsof", "-ti", f":{ws_port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=2,
            )
            if r.stdout.strip():
                if is_windows:
                    tail = (
                        "[dim]看不到？右键系统托盘 Spirit 图标 → 显 示/隐藏[/dim]\n"
                        "[dim]仍看不到？托盘图标可能在「隐藏的图标」里，点右下小箭头展开[/dim]"
                    )
                else:
                    tail = (
                        "[dim]看不到？检查 Dock 或 Cmd+Tab 切换窗口[/dim]\n"
                        "[dim]仍看不到？托盘菜单 → 重置位置[/dim]"
                    )
                console.print(Panel(
                    f"[bold green]小狐狸已上桌[/bold green]\n"
                    f"WebSocket: [cyan]ws://127.0.0.1:{ws_port}[/cyan]\n\n"
                    + tail,
                    border_style="green",
                ))
                return
        except Exception:
            pass

    console.print(Panel(
        f"[bold yellow]桌宠已启动但 10s 内未检测到 9877 端口[/bold yellow]\n"
        f"[dim]请查看 desktop-app 日志或手动检查：lsof -iTCP:{ws_port} -sTCP:LISTEN[/dim]",
        border_style="yellow",
    ))
    raise click.exceptions.Exit(code=1)


def _spawn_electron_only(wait: bool, ws_port: int, is_windows: bool) -> None:
    """状态 C 用：launcher 已健康，只重新 spawn Electron 接到现成 launcher。"""
    project_root = _resolve_spirit_project_root()
    if project_root is None:
        console.print(Panel(
            "[bold red]找不到 spirit_agent 项目根[/bold red]\n"
            "[dim]无法单独 spawn Electron。[/dim]",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)
    electron_bin = project_root / "desktop-app" / "node_modules" / ".bin" / "electron"
    electron_main = project_root / "desktop-app" / "dist-electron" / "main.js"
    if not electron_bin.exists() or not electron_main.exists():
        console.print(Panel(
            f"[bold red]未找到 electron 二进制或 main.js[/bold red]\n",
            border_style="red",
        ))
        raise click.exceptions.Exit(code=1)
    _spawn_electron_and_wait(
        wait=wait, ws_port=ws_port, is_windows=is_windows,
        project_root=project_root,
        electron_bin=electron_bin, electron_main=electron_main,
    )


# ── v0.4.0 storage subcommand group ─────────────────────────────
# Hermes-aligned CLI surface for the new storage mixins: search / history /
# undo / stats / export / health / prune / vacuum.
@cli.group()
def storage():
    """SQLite 存储层运维命令（v0.4.0 Hermes-aligned）"""


def _storage_open() -> Any:
    """Open SessionDB with graceful error reporting."""
    from spirit.storage.session_db import SessionDB
    try:
        return SessionDB()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]无法打开 state.db: {exc}[/red]")
        sys.exit(1)


@storage.command("search")
@click.argument("query")
@click.option("--limit", "-n", default=20, help="结果上限")
@click.option("--session", "-s", default=None, help="限定 session_id")
def cmd_storage_search(query: str, limit: int, session: Optional[str]) -> None:
    """全量检索 messages (FTS5 + LIKE fallback)"""
    from .storage_commands import cmd_search

    db = _storage_open()
    result = cmd_search(db, query=query, limit=limit, session_id=session)
    if not result.get("ok"):
        console.print(f"[red]搜索失败: {result.get('error')}[/red]")
        db.close()
        sys.exit(1)
    hits = result.get("hits", [])
    console.print(f"[bold cyan]查询：[/bold cyan] {query}  [dim]→ {len(hits)} 条命中[/dim]")
    for h in hits:
        sid = (h.get("session_id") or "")[:8]
        console.print(
            f"  [{sid}] {h.get('role'):9} | {h.get('preview', '')[:80]}"
        )
    db.close()


@storage.command("history")
@click.argument("session_id")
@click.option("--limit", "-n", default=50, help="时间线条目上限")
def cmd_storage_history(session_id: str, limit: int) -> None:
    """列出某个 session 的 timeline 视图"""
    from .storage_commands import cmd_history

    db = _storage_open()
    result = cmd_history(db, session_id=session_id, limit=limit)
    if not result.get("ok"):
        console.print(f"[red]history 失败: {result.get('error')}[/red]")
        db.close()
        sys.exit(1)
    summary = result.get("summary") or {}
    console.print(
        f"[bold cyan]Session {session_id[:8]}[/bold cyan]  "
        f"msgs={summary.get('total_messages', '?')} "
        f"tools={summary.get('total_tool_calls', '?')}"
    )
    for entry in result.get("entries", []):
        console.print(f"  {entry}")
    db.close()


@storage.command("undo")
@click.argument("session_id")
@click.option("--to-message", default=None, type=int,
              help="跳到此 message_id 之前")
def cmd_storage_undo(session_id: str, to_message: Optional[int]) -> None:
    """/undo：rewind session 到指定 user message 前"""
    from .storage_commands import cmd_undo

    db = _storage_open()
    result = cmd_undo(
        db, session_id=session_id,
        target_user_message_id=to_message,
    )
    if not result.get("ok"):
        console.print(f"[red]undo 失败: {result.get('error')}[/red]")
        db.close()
        sys.exit(1)
    console.print(f"[green]rewind 完成:[/green] {result.get('outcome')}")
    db.close()


@storage.command("stats")
def cmd_storage_stats() -> None:
    """DB 结构 + 健康快照"""
    from .storage_commands import cmd_stats

    db = _storage_open()
    result = cmd_stats(db)
    console.print_json(data=result)
    db.close()


@storage.command("health")
def cmd_storage_health() -> None:
    """检查 storage 健康 / corrupt 标记"""
    from .storage_commands import cmd_health

    db = _storage_open()
    result = cmd_health(db)
    console.print_json(data=result)
    db.close()


@storage.command("vacuum")
@click.confirmation_option(prompt="确认执行 VACUUM？这会重建 DB 文件。")
def cmd_storage_vacuum() -> None:
    """压缩 / 重整 DB 文件"""
    from .storage_commands import cmd_vacuum

    db = _storage_open()
    result = cmd_vacuum(db)
    console.print_json(data=result)
    db.close()


@storage.command("prune")
@click.option("--older-than-days", default=90, type=int,
              help="删除 N 天前的行")
@click.confirmation_option(prompt="确认删除旧消息？")
def cmd_storage_prune(older_than_days: int) -> None:
    """清理 N 天前的 messages（软删除）"""
    from .storage_commands import cmd_prune

    db = _storage_open()
    result = cmd_prune(db, older_than_days=older_than_days)
    console.print_json(data=result)
    db.close()


@storage.command("export")
@click.option("--output", "-o", required=True, type=click.Path(),
              help="导出文件路径 (JSON)")
def cmd_storage_export(output: str) -> None:
    """把 messages + sessions 导出为 JSON 快照"""
    from .storage_commands import cmd_export

    db = _storage_open()
    result = cmd_export(db, Path(output))
    if not result.get("ok"):
        console.print(f"[red]export 失败: {result.get('error')}[/red]")
        db.close()
        sys.exit(1)
    console.print(f"[green]已导出到 {output}[/green]")
    db.close()


if __name__ == "__main__":
    cli()
