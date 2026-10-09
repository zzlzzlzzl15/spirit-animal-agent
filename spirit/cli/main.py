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
from typing import Optional

import click
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

console = Console()


# ---------------------------------------------------------------------------
# CLI 渲染辅助 — 顶部欢迎面板 + 历史会话 + 底部状态栏
# ---------------------------------------------------------------------------
# 设计语言:
#   - 主色: yellow (Spirit 小狐狸主题)
#   - 折叠: dim 灰色边框 + 小标题
#   - 顶部欢迎面板: HEAVY 边框 + 🦊 图标
#   - 状态栏: 'state | ⚡ model | 🕐 time | 💬 msgs | 🔧 tools' + 绿点
#   - 历史任务: [n] 编号 + 第一条用户消息前 50 字
#   - 思考中: Live(Spinner) 显示，屏蔽所有内部回调
# ---------------------------------------------------------------------------

WELCOME_PANEL = """\
[bold yellow]🦊 Spirit Agent CLI v1.0[/bold yellow]

[white]输入[/white] [bold]/help[/bold] [white]查看可用命令[/white]
[white]直接输入文字与[/white] [bold]Agent[/bold] [white]对话[/white]"""

RECENT_HEADER = Text()
RECENT_HEADER.append("—— 最近任务（点击编号或输入 ", style="dim")
RECENT_HEADER.append("[n]", style="yellow")
RECENT_HEADER.append(" 恢复并继续，/tasks 重显）——", style="dim")

STATUS_BAR_IDLE = (
    "  [dim]空闲[/dim]  |  [yellow]⚡ {model}[/yellow]  |  "
    "[cyan]🕐 {elapsed}s[/cyan]  |  [magenta]💬 {msgs}[/magenta]  |  "
    "[green]🔧 {tools}[/green]"
)
STATUS_BAR_THINKING = (
    "  [bold yellow]思考中...[/bold yellow]  |  [yellow]⚡ {model}[/yellow]  |  "
    "[cyan]🕐 {elapsed}s[/cyan]  |  [magenta]💬 {msgs}[/magenta]  |  "
    "[green]🔧 {tools}[/green]"
)

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
    """配置日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


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

    # --- 隐藏所有内部思考 / 工具调用同调 ---
    # 思考过程不显示，只看到最终响应 + spinner。
    # tool 回调 / reasoning stream 都置为 no-op，避免 UI 噪声。
    def _silent(*_args, **_kwargs):
        pass

    agent.on_tool_start = _silent
    agent.on_tool_complete = _silent
    if getattr(agent, "on_stream_delta", None) is not None:
        agent.on_stream_delta = _silent

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
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.styles import Style

        prompt_style = Style.from_dict({
            "prompt": "ansiyellow bold",
            "indicator": "ansiyellow",
        })

        # 会话状态容器
        class _State:
            thinking: bool = False
            elapsed: float = 0.0

        state = _State()

        def _bottom_toolbar():
            template = STATUS_BAR_THINKING if state.thinking else STATUS_BAR_IDLE
            return HTML(template.format(
                model=agent.model or "?",
                elapsed=int(state.elapsed),
                msgs=len(agent.messages),
                tools=len(agent.get_tool_definitions()),
            ))

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
        state = type("_S", (), {"thinking": False, "elapsed": 0.0})()

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

            # --- 对话：Spinner 表示思考中，内部回调全部静默 ---
            state.thinking = True
            t0 = time.time()
            spinner_text = Text()
            spinner_text.append("spirit 正在思考", style="bold yellow")
            spinner_text.append("  •  ", style="dim")
            spinner_text.append(agent.model or "?", style="yellow")

            # 启动计时线程以刷新底部 elapsed
            stop_tick = threading.Event()

            def _tick():
                while not stop_tick.is_set():
                    state.elapsed = time.time() - t0
                    time.sleep(0.5)

            tick_thread = threading.Thread(target=_tick, daemon=True)
            tick_thread.start()

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

            # --- 只显示最终响应（不含 thinking 内部过程）---
            if result.response:
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

    # 结束
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
