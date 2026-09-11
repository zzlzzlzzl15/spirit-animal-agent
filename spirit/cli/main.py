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

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()


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

    # 工具回调
    def on_tool_start(name, args):
        console.print(f"  [dim]🔧 {name}({args})[/dim]")

    def on_tool_complete(name, result):
        preview = result[:100] + "..." if len(result) > 100 else result
        console.print(f"  [dim]   → {preview}[/dim]")

    agent.on_tool_start = on_tool_start
    agent.on_tool_complete = on_tool_complete

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
                if _handle_slash_command(user_input, agent):
                    continue
                else:
                    break

            # 对话
            console.print()
            result = agent.run_conversation(user_input)

            # 显示响应
            if result.response:
                console.print(f"[bold blue]Spirit>[/bold blue]")
                console.print(Markdown(result.response))

            # 显示 token 使用
            if result.usage:
                total = result.usage.get("total_tokens", 0)
                console.print(f"  [dim]tokens: {total} | iterations: {result.iterations}[/dim]")

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
