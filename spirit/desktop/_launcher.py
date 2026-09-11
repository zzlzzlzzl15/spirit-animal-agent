"""Spirit Desktop Pet — Python 后端启动器。

由 Electron 主进程通过 child_process.spawn 调用。
启动 WebSocket 桥接服务器 + PetEngine。
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("spirit.desktop.launcher")


def main():
    parser = argparse.ArgumentParser(description="Spirit Desktop Pet 后端")
    parser.add_argument("--ws-host", default="127.0.0.1", help="WebSocket 绑定地址")
    parser.add_argument("--ws-port", type=int, default=9877, help="WebSocket 端口")
    args = parser.parse_args()

    # 确保项目根目录在 sys.path 中
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from spirit.desktop.pet_engine import PetEngine
    from spirit.desktop.ws_server import WSServer
    from spirit.agent.agent import AgentConfig, SpiritAgent
    from spirit.config import load_config

    # 创建引擎
    engine = PetEngine()
    logger.info("PetEngine 已初始化")

    # 初始化 Agent（从配置文件加载 LLM 配置）
    agent = None
    try:
        raw = load_config()
        llm_cfg = raw.get("llm", {})
        config = AgentConfig(
            model=llm_cfg.get("model", ""),
            api_key=llm_cfg.get("api_key", ""),
            base_url=llm_cfg.get("base_url", ""),
            provider=llm_cfg.get("provider", "auto"),
            platform="cli",
        )
        agent = SpiritAgent(config)
        logger.info("SpiritAgent 已初始化: model=%s, provider=%s", config.model, config.provider)
    except Exception as exc:
        logger.warning("Agent 初始化失败（CLI 对话功能将不可用）: %s", exc)

    # 创建 WebSocket 服务器
    server = WSServer(engine, host=args.ws_host, port=args.ws_port, agent=agent)

    # 事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 优雅退出
    def shutdown():
        logger.info("正在关闭...")
        loop.run_until_complete(server.stop())
        loop.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, lambda *_: shutdown())
    signal.signal(signal.SIGTERM, lambda *_: shutdown())

    # 启动
    loop.run_until_complete(server.start())
    logger.info("WebSocket 服务器运行中: ws://%s:%d", args.ws_host, args.ws_port)
    print(f"READY ws://{args.ws_host}:{args.ws_port}", flush=True)

    # 保持运行
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
