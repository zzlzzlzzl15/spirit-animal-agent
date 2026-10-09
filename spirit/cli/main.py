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
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

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


# ---------------------------------------------------------------------------
# 阶段进度事件渲染（register_progress_sink 的 sink 回调）
# ---------------------------------------------------------------------------
# 在 spirit.cli.chat() 里调 register_progress_sink(_render_progress) 生效；
# conversation_loop 推送 7 阶段，本函数负责逐阶段渲染到 console（不在
# Live(Spinner) 里渲染——避免 spinner 刷新覆盖阶段标题、阶段行产生越级）。
# ---------------------------------------------------------------------------

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
        duration_ms = extra.get("duration_ms")
        if phase == ProgressPhase.TASK_START.value:
            console.print(f"  [bold yellow]─── 任务开始[/bold yellow]  [dim]{_truncate(summary, 70)}[/dim]")
        elif phase == ProgressPhase.THINKING.value:
            # 思考开始——不输出独立行（Live(Spinner) 正在显示），仅记录迭代
            pass
        elif phase == ProgressPhase.THINKING_END.value:
            if duration_ms is not None:
                console.print(
                    f"  [dim]·[/dim] [yellow]推理完成[/yellow] [dim]({duration_ms / 1000:.1f}s，第 {iterations} 轮)[/dim]"
                )
        elif phase == ProgressPhase.TOOL_CALL.value:
            count = extra.get("count", 0) or 0
            console.print(
                f"  [dim]·[/dim] [cyan]调用工具[/cyan] [bold]{summary}[/bold] [dim]({count} 个)[/dim]"
            )
        elif phase == ProgressPhase.TOOL_RESULT.value:
            ms = duration_ms if duration_ms is not None else 0
            # TOOL_RESULT 与 TOOL_CALL 的 summary 都是工具名拼接
            console.print(
                f"  [green]✓[/green] [dim]完成[/dim] [bold]{summary}[/bold] [dim]({ms / 1000:.1f}s)[/dim]"
            )
        elif phase == ProgressPhase.TASK_COMPLETE.value:
            reason = extra.get("reason", "")
            tail = f"  [dim]({reason})[/dim]" if reason else ""
            console.print(
                f"  [bold green]─── 任务完成[/bold green]  [dim]({iterations} 轮)[/dim]{tail}"
            )
        elif phase == ProgressPhase.TASK_ABORTED.value:
            reason = extra.get("reason", "unknown")
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
    """顶部黄色欢迎面板 — Spirit CLI v1.0 + 小狐狸 + 命令提示。"""
    body = Text()
    body.append(FOX_LOGO, style="bold yellow")
    body.append("\n")
    body.append("Spirit Agent CLI ", style="bold yellow")
    body.append("v1.0\n\n", style="dim yellow")
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
        # 取首条用户消息作为预览
        try:
            msgs = db.get_messages(s["id"])
        except Exception:
            msgs = []
        preview = ""
        for m in msgs:
            if m.get("role") == "user" and m.get("content"):
                preview = m["content"].replace("\n", " ").strip()
                break
        if not preview:
            preview = "(空会话)"
        if len(preview) > 50:
            preview = preview[:50] + "..."
        started = s.get("started_at", "")[:19]  # 截掉毫秒

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


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="详细日志")
@click.pass_context
def cli(ctx, verbose):
    """Spirit Agent — 统一后端 AI Agent 服务"""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    setup_logging(verbose)


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

            字段顺序：状态 | 模型 | ctx% (详情) | Σ 会话用量 (in out, calls) | 时长 | #msgs | Ttools
            """
            if state.thinking:
                head = [("class:status-bar-thinking", "思考中")]
            else:
                head = [("class:status-bar-strong", "空闲")]
            model_txt = (agent.model or "?").strip()
            if len(model_txt) > 26:
                model_txt = model_txt[:23] + "..."
            elapsed_txt = _fmt_duration(state.elapsed)
            tool_summary = state.last_tools or ""
            if len(tool_summary) > 24:
                tool_summary = tool_summary[:21] + "..."
            msgs = len(agent.messages)
            tools = len(agent.get_tool_definitions())

            # ── 上下文 % / 详情（从 context_compressor 拿）──
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
                        # 阈值色：绿 ≤50 / 黄 50-80 / 红 >80
                        if pct > 80:
                            ctx_class = "class:status-bar-bad"
                        elif pct > 50:
                            ctx_class = "class:status-bar-warn"
                        else:
                            ctx_class = "class:status-bar-good"
                        ctx_frag = [
                            ("class:status-bar-dim", "  |  "),
                            (ctx_class, f"ctx {pct}%"),
                            ("class:status-bar-dim", " ("),
                            ("class:status-bar", f"{_fmt_tokens(tokens)}/{_fmt_tokens(ctx_len)}"),
                            ("class:status-bar-dim", ")"),
                        ]
            except Exception:
                ctx_frag = None

            # ── Σ 会话用量：in + out + API calls（从 agent.session_* 拿）──
            try:
                in_tok = int(getattr(agent, "session_input_tokens", 0) or 0)
                out_tok = int(getattr(agent, "session_output_tokens", 0) or 0)
                total_tok = int(getattr(agent, "session_total_tokens", 0) or 0)
                api_calls = int(getattr(agent, "session_api_calls", 0) or 0)
            except Exception:
                in_tok = out_tok = total_tok = api_calls = 0

            sum_frag = None
            if total_tok > 0 or api_calls > 0:
                sum_frag = [
                    ("class:status-bar-dim", "  |  "),
                    ("class:status-bar-tool", f"Σ {_fmt_tokens(total_tok)}"),
                    ("class:status-bar-dim", " ("),
                    ("class:status-bar", f"in {_fmt_tokens(in_tok)}"),
                    ("class:status-bar-dim", " "),
                    ("class:status-bar", f"out {_fmt_tokens(out_tok)}"),
                    ("class:status-bar-dim", ", "),
                    ("class:status-bar", f"{api_calls} calls"),
                    ("class:status-bar-dim", ")"),
                ]

            frags = [
                ("class:status-bar-dim", "  "),
                *head,
                ("class:status-bar-dim", "  |  "),
                ("class:status-bar", model_txt),
            ]
            if ctx_frag:
                frags.extend(ctx_frag)
            if sum_frag:
                frags.extend(sum_frag)
            frags.extend([
                ("class:status-bar-dim", "  |  "),
                ("class:status-bar-tool", elapsed_txt),
                ("class:status-bar-dim", "  |  "),
                ("class:status-bar", "tools: "),
                ("class:status-bar-tool", tool_summary or "-"),
                ("class:status-bar-dim", "  |  "),
                ("class:status-bar-tool", f"#{msgs}"),
                ("class:status-bar-dim", "  |  "),
                ("class:status-bar-tool", f"T{tools}"),
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
                        f"{s['source']} | {s['model']} | {s['started_at']}"
                    )
            else:
                console.print("  [dim]暂无会话记录[/dim]")
            db.close()
        except Exception as e:
            console.print(f"  [red]查询失败: {e}[/red]")

    elif command == "/pet":
        # 启动/唤起桌面小狐狸。
        # 桌宠是 Electron 应用 + Python 后端（端口 9877）。
        # 检测：端口 9877 是否已监听 → Electron 已在跑。
        import subprocess
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetTCPConnection -State Listen -LocalPort 9877 "
                 "-ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty OwningProcess"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            running_pid = r.stdout.strip()
        except Exception:
            running_pid = ""

        if running_pid:
            # 后端已在跑 → 桌宠 Electron 也在跑。
            # 提示用户；如果窗口被隐藏，告诉他右键托盘「显示/隐藏」。
            console.print(Panel(
                "[bold yellow]小狐狸已经在桌面上[/bold yellow]\n"
                f"WebSocket 端口 9877 已监听（Python 后端 PID [cyan]{running_pid}[/cyan]）\n\n"
                "[dim]如果桌面看不到：[/dim]\n"
                "[dim]  - 右键系统托盘的 Spirit 图标 → 显示/隐藏[/dim]\n"
                "[dim]  - 托盘图标可能在“隐藏的图标”里，点系统托盘右下小箭头展开[/dim]",
                border_style="yellow",
            ))
        else:
            # 后端不在 → 调 'spirit pet' 启动 Electron 桌宠（新窗口）。
            # 路径解析：优先在当前 PATH + 用户级 PATH 找 spirit.cmd，
            # 兑底硬编码路径（PowerShell 进程 $env:PATH 可能没继承用户 PATH）。
            import shutil
            spirit_path = (
                shutil.which("spirit.cmd")
                or shutil.which("spirit")
                or shutil.which("spirit.cmd", path=r"C:\Users\Administrator\bin;" + os.environ.get("PATH", ""))
                or r"C:\Users\Administrator\bin\spirit.cmd"
            )
            try:
                subprocess.Popen(
                    [spirit_path, "pet"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                console.print(Panel(
                    "[bold green]小狐狸已启动到桌面[/bold green]\n\n"
                    "[dim]查看新打开的窗口（小狐狸会出现在桌面上）。[/dim]\n"
                    "[dim]关闭：右键系统托盘的 Spirit 图标 → 退出[/dim]",
                    border_style="green",
                ))
            except FileNotFoundError:
                console.print(Panel(
                    f"[bold red]找不到 'spirit' 命令[/bold red]\n"
                    f"[dim]尝试了路径: {spirit_path}\n"
                    "[dim]请检查 C:\\Users\\Administrator\\bin 是否在 PATH 中，"
                    "或 spirit.cmd 是否存在。[/dim]",
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
                    f"{s['started_at']}"
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


if __name__ == "__main__":
    cli()
