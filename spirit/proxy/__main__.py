"""``python -m spirit.proxy`` —— 前台启动 OpenAI 兼容代理服务器。

读取 ``SPIRIT_PROXY_*`` 环境变量 / config.yaml 的 ``proxy.*`` 段决定 host/port/key/model，
然后阻塞式运行（Ctrl-C 退出）。示例::

    SPIRIT_PROXY_ENABLED=true SPIRIT_PROXY_PORT=8642 python -m spirit.proxy
    # 前端指向 http://127.0.0.1:8642/v1
"""

from __future__ import annotations

import logging

from spirit.proxy.config import load_proxy_config
from spirit.proxy.server import ProxyServer


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_proxy_config()
    server = ProxyServer(config=cfg)
    print(f"Spirit OpenAI 兼容代理: {server.url}/v1  (model={cfg.model})")
    if not cfg.requires_auth():
        print("⚠ 未设置 SPIRIT_PROXY_KEY —— 无鉴权，仅建议 localhost 绑定使用。")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
