"""Spirit Agent CLI — 命令行入口（增强版）。

用法：
    spirit chat          交互式对话
    spirit start         启动后端服务
    spirit sessions      查看会话历史
    spirit tools         查看可用工具
"""

import logging
import os
import sys
import re

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


def setup_logging(verbose: bool = False):
    """配置日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _clean_think_tags(text: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text.
    
    Direct copy from Hermes agent_runtime_helpers.strip_think_blocks()
    Handles:
      1. Closed tag pairs (...)
      2. Unterminated open tag at block boundary
      3. Stray orphan tags
    """
    if not text:
        return ""
    
    # 1. Closed tag pairs — case-insensitive
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # 1b. Tool call XML blocks
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # 2. Unterminated reasoning block — open tag at a block boundary
    #    (start of text, or after a newline) with no matching close.
    #    Strip from the tag to end of LINE (not end of string).
    text = re.sub(
        r'(?:^|\n)[ \t]*<(?:think|thinking|reasoning)\b[^>]*>[^\n]*',
        '',
        text,
        flags=re.IGNORECASE,
    )
    
    # 3. Stray orphan open/close tags that slipped through.
    text = re.sub(
        r'</?(?:think|thinking|reasoning)>\s*',
        '',
        text,
        flags=re.IGNORECASE,
    )
    
    return text.strip()
def _format_tool_calls_display(tool_calls: list) -> str:
    """将工具调用列表格式化为友好显示。"""
    if not tool_calls:
        return ""
    
    lines = []
    for call in tool_calls:
        # Handle both formats:
        # 1. {"name": "...", "arguments": {...}} (simplified)
        # 2. {"id": "...", "type": "function", "function": {"name": "...", "arguments": {...}}} (OpenAI style)
        if "function" in call:
            # OpenAI style
            func = call["function"]
            name = func.get("name", "unknown")
            args = func.get("arguments", {})
        else:
            # Simplified style
            name = call.get("name", "unknown")
            args = call.get("arguments", {})
        
        # 简化参数显示
        if isinstance(args, dict):
            args_str = ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:3])
            if len(args) > 3:
                args_str += f", ... (+{len(args)-3} more)"
        else:
            args_str = str(args)[:100]
        
        lines.append(f"  [TOOL] {name}({args_str})")
    
    return "\n".join(lines)


def _make_cli_approval_callback():
    """构建 CLI 交互式危险命令审批回调。

    签名: (command, description, pattern_key) -> "deny"/"session"/"always"。
    在终端弹出安全审批面板，让用户选择本次放行 / 总是放行 / 拒绝。
    """
    def _callback(command: str, description: str, pattern_key: str) -> str:
        console.print(Panel.fit(
            f"[red]{command}[/red]\n\n原因: [cyan]{description}[/cyan]",
            title="[bold yellow]⚠ 危险命令需审批[/bold yellow]",
            border_style="yellow",
        ))
        try:
            ans = console.input(
                "允许执行? [bold]y[/bold]=本次 / [bold]a[/bold]=总是 / [bold]n[/bold]=拒绝（默认 n）: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("a", "always"):
            return "always"
        if ans in ("y", "yes", "session"):
            return "session"
        return "deny"
    return _callback


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
    # （terminal 工具执行危险命令前会征求用户意见，而非静默阻止）
    from spirit.tools.approval import set_approval_callback, set_interactive_context
    set_approval_callback(_make_cli_approval_callback())
    set_interactive_context(True)

    # 欢迎信息
    console.print(Panel.fit(
        f"[bold green]Spirit Agent[/bold green] v0.1.0\n"
        f"模型: [cyan]{agent.model}[/cyan]\n"
        f"工具: [cyan]{len(agent.get_tool_definitions())}[/cyan] 个可用\n"
        f"输入 [bold]/help[/bold] 查看命令，[bold]/quit[/bold] 退出",
        border_style="green",
    ))

    # 交互循环
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        session = PromptSession(history=InMemoryHistory())
    except ImportError:
        session = None

    while True:
        try:
            if session:
                user_input = session.prompt("\nYou> ")
            else:
                user_input = input("\nYou> ")

            user_input = user_input.strip()
            if not user_input:
                continue

            # 斜杠命令
            if user_input.startswith("/"):
                # 动态技能 / 捆绑 slash 命令（/<skill-name> [指令]）解析为调用消息后
                # 落到下面的对话路径；解析不到（含 /skill 等固定命令）走固定命令处理。
                skill_message = _try_resolve_skill_slash(user_input)
                if skill_message is not None:
                    user_input = skill_message
                elif _handle_slash_command(user_input, agent):
                    continue
                else:
                    break

            # 对话
            console.print()
            
            # 显示思考过程开始
            console.print("[dim italic] Spirit Agent 正在思考...[/dim italic]")
            
            result = agent.run_conversation(user_input)

            # 清理响应文本（移除 <think> 标签）
            response_text = _clean_think_tags(result.response) if result.response else ""

            # 显示本轮的工具调用（如果有）
            if hasattr(result, 'tool_calls') and result.tool_calls:
                console.print(f"\n[bold yellow]🔧 工具调用 ({len(result.tool_calls)}):[/bold yellow]")
                for call in result.tool_calls:
                    name = call.get("name", "unknown")
                    args = call.get("arguments", {})
                    
                    # 简化参数显示
                    if isinstance(args, dict):
                        args_str = ", ".join(f"{k}={repr(v)[:50]}" for k, v in list(args.items())[:3])
                        if len(args) > 3:
                            args_str += f", ... (+{len(args)-3} more)"
                    else:
                        args_str = str(args)[:100]
                    
                    console.print(f"  🔹 {name}({args_str})")

            # 显示响应
            if response_text:
                console.print(f"\n[bold blue]💡 Spirit Agent:[/bold blue]")
                console.print(Markdown(response_text))

            # 显示 token 使用
            if result.usage:
                total = result.usage.get("total_tokens", 0)
                console.print(f"\n[dim]tokens: {total} | iterations: {result.iterations}[/dim]")

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
            "  /goal        持久目标（Ralph Loop）: /goal <text> | status | show | pause | resume | clear | draft <obj> | wait <pid> | unwait\n"
            "  /subgoal     子目标: /subgoal <text> | remove <n> | clear\n"
            "  /moa         智囊团（Mixture of Agents）: /moa list | use <name> | off | <prompt>（一次性）\n"
            "  /skill       技能中心: /skill list | reload | browse | install <id> | uninstall <name> | scan | bundles | info <name> | audit\n"
            "  /<skill>     调用技能: /<skill-name> [指令] · 捆绑: /<bundle-name> [指令]\n"
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

    elif command == "/goal":
        _handle_goal_slash(agent, args)

    elif command == "/subgoal":
        from spirit.goals import handle_subgoal_command
        result = handle_subgoal_command(agent, args)
        _render_goal_result(result)

    elif command == "/moa":
        _handle_moa_slash(agent, args)

    elif command in ("/skill", "/skills"):
        from spirit.skills_hub import handle_skill_command
        result = handle_skill_command(agent, args)
        _render_skill_result(result)

    else:
        console.print(f"[red]未知命令: {command}[/red] 输入 /help 查看帮助")

    return True


def _render_goal_result(result: dict) -> None:
    """把 handle_goal_command / handle_subgoal_command 的结果 dict 渲染到 Rich console。"""
    msg = result.get("message", "")
    style = "green" if result.get("ok") else "yellow"
    if msg:
        console.print(f"  [{style}]{msg}[/{style}]")
    for line in result.get("lines", []):
        if line:
            console.print(f"  [dim]{line}[/dim]")


def _render_skill_result(result: dict) -> None:
    """把 handle_skill_command 的结果 dict 渲染到 Rich console。"""
    msg = result.get("message", "")
    style = "green" if result.get("ok") else "yellow"
    if msg:
        console.print(f"  [{style}]{msg}[/{style}]")
    for line in result.get("lines", []):
        if line:
            console.print(f"  [dim]{line}[/dim]")


def _try_resolve_skill_slash(user_input: str):
    """把 ``/<skill-name>`` 或 ``/<bundle>`` [指令] 解析为技能调用消息（解析不到 None）。

    仅拦截能解析为已安装技能 / 捆绑的命令；``/skill`` 等保留命令（不在技能命令面）
    返回 None，交给固定命令处理。任何异常都降级为 None（绝不因技能系统拖垮 CLI）。
    """
    parts = user_input.split(maxsplit=1)
    command = parts[0].lstrip("/").lower()
    rest = parts[1] if len(parts) > 1 else ""
    if not command:
        return None
    try:
        from spirit.skills_hub import resolve_slash_skill_or_bundle

        return resolve_slash_skill_or_bundle(command, rest)
    except Exception:
        return None


def _handle_moa_slash(agent, args: str) -> None:
    """处理 /moa：派发 + 渲染。一次性模式（/moa <prompt>）会阻塞跑一轮 MoA。

    参考输出通过 ``_moa_reference_callback`` 实时渲染（每个 advisor 完成一块），
    聚合器最终响应作为 Markdown 打印。list/use/off 为纯状态流转，不触网。
    """
    from spirit.moa import handle_moa_command

    # 装一个参考展示钩子：MoA fan-out 每完成一个 advisor 就打印其输出块。
    prev_cb = getattr(agent, "_moa_reference_callback", None)
    agent._moa_reference_callback = _render_moa_reference_event
    try:
        result = handle_moa_command(agent, args)
    finally:
        agent._moa_reference_callback = prev_cb
    _render_moa_result(result)


def _render_moa_reference_event(event: str, **kwargs) -> None:
    """把 MoAClient 发射的 moa.reference / moa.aggregating 事件渲染到 console。

    事件 kwargs 契约（见 moa_loop.MoAChatCompletions._emit）：
      moa.reference   → index, count, label, text
      moa.aggregating → aggregator(label), ref_count
    """
    try:
        if event == "moa.reference":
            label = kwargs.get("label", "?")
            idx = kwargs.get("index")
            count = kwargs.get("count")
            head = f"🎭 参考[{label}]"
            if idx is not None and count:
                head = f"🎭 参考 {idx}/{count}[{label}]"
            console.print(f"  [magenta]{head}:[/magenta]")
            snippet = (kwargs.get("text", "") or "").strip()
            if len(snippet) > 1200:
                snippet = snippet[:1200] + " …"
            if snippet:
                console.print(f"  [dim]{snippet}[/dim]")
        elif event == "moa.aggregating":
            agg = kwargs.get("aggregator", "?")
            ref_count = kwargs.get("ref_count", 0)
            console.print(f"  [cyan]🎭 聚合器[{agg}] 正在综合 {ref_count} 条参考并行动…[/cyan]")
    except Exception:
        pass


def _render_moa_result(result: dict) -> None:
    """把 handle_moa_command 的结果 dict 渲染到 Rich console。"""
    msg = result.get("message", "")
    style = "green" if result.get("ok") else "yellow"
    if msg:
        console.print(f"  [{style}]{msg}[/{style}]")
    for line in result.get("lines", []):
        if line:
            console.print(f"  [dim]{line}[/dim]")
    response = result.get("response")
    if response:
        console.print("\n[bold blue]🎭 MoA 聚合响应:[/bold blue]")
        console.print(Markdown(_clean_think_tags(response)))


def _handle_goal_slash(agent, args: str) -> None:
    """处理 /goal：派发 + 渲染，必要时驱动 Ralph Loop（对齐 Hermes "kick the loop off"）。"""
    from spirit.goals import handle_goal_command

    result = handle_goal_command(agent, args)
    _render_goal_result(result)

    kick_off = result.get("kick_off")
    if not kick_off or not result.get("ok"):
        return
    # 设完/恢复目标后立即驱动 Ralph Loop，免得用户再发一条消息。
    _drive_goal_loop(agent, kick_off)


def _drive_goal_loop(agent, first_input: str) -> None:
    """在当前进程驱动 Ralph Loop，直到 done / paused / waiting / 预算耗尽。"""
    from spirit.goals import run_goal_turn_loop

    console.print("\n[dim italic] Spirit Agent 正在朝目标自主推进（Ralph Loop）...[/dim italic]")

    def _on_turn(text, idx):
        cleaned = _clean_think_tags(text) if text else ""
        if cleaned:
            console.print(f"\n[bold blue]💡 Spirit Agent（第 {idx} 轮）:[/bold blue]")
            console.print(Markdown(cleaned))

    def _on_decision(decision):
        dmsg = decision.get("message", "")
        if dmsg:
            console.print(f"  [dim]{dmsg}[/dim]")

    try:
        result = run_goal_turn_loop(
            agent, first_input, on_turn=_on_turn, on_decision=_on_decision
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断目标循环（目标仍保留，可 /goal resume 继续）[/yellow]")
        try:
            agent.interrupt()
        except Exception:
            pass
        return
    except Exception as exc:
        console.print(f"  [red]目标循环出错: {exc}[/red]")
        return

    outcome = result.get("outcome", "")
    turns = result.get("turns_used", 0)
    reason = result.get("reason", "")
    tail = f" — {reason}" if reason else ""
    console.print(f"\n[dim]目标循环结束: outcome={outcome}, turns={turns}{tail}[/dim]")


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
