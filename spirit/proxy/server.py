"""代理 HTTP 服务器 —— stdlib ``http.server`` 薄适配层。

:mod:`spirit.proxy.handler` 承载全部路由/格式逻辑（传输无关、可离线测）。本模块只做
socket ↔ handler 的桥接：读请求行/头/体 → 调 :meth:`ProxyHandler.handle` → 写响应。

用标准库 ``ThreadingHTTPServer``，**无第三方依赖**（不引入 aiohttp/fastapi）。

用法::

    from spirit.proxy import ProxyServer, load_proxy_config
    cfg = load_proxy_config()
    server = ProxyServer(config=cfg)
    server.start()          # 后台线程
    ...
    server.stop()

    # 或阻塞式：
    server.serve_forever()
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from spirit.proxy.config import ProxyConfig, load_proxy_config
from spirit.proxy.handler import ProxyHandler, ProxyResponse

logger = logging.getLogger(__name__)


class _RequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求翻译成 :class:`ProxyHandler` 调用。"""

    # 由 ProxyServer 注入（类属性，供所有请求共享）
    proxy_handler: ProxyHandler = None  # type: ignore[assignment]
    server_version = "SpiritProxy/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        body = self._read_body()
        headers = {k: v for k, v in self.headers.items()}
        response: ProxyResponse = self.proxy_handler.handle(
            method, self.path, headers, body
        )
        self._write(response)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except Exception as exc:  # pragma: no cover - 网络读取异常
            logger.debug("读取请求体失败: %s", exc)
            return b""

    def _write(self, response: ProxyResponse) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            try:
                self.wfile.write(response.body)
            except Exception as exc:  # pragma: no cover - 客户端断连
                logger.debug("写响应失败: %s", exc)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib 签名
        """把 stdlib 访问日志路由到 logging（默认 stderr 太吵）。"""
        logger.debug("proxy %s - %s", self.address_string(), fmt % args)


class ProxyServer:
    """OpenAI 兼容代理服务器生命周期封装。"""

    def __init__(
        self,
        config: Optional[ProxyConfig] = None,
        completion_fn=None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.config = config if config is not None else load_proxy_config()
        self.handler = ProxyHandler(config=self.config, completion_fn=completion_fn)
        self._host = host or self.config.host
        self._port = port if port is not None else self.config.port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------

    def _make_httpd(self) -> ThreadingHTTPServer:
        # 用 type() 派生一个绑定了本 handler 的子类，避免全局可变状态。
        handler_cls = type(
            "BoundRequestHandler", (_RequestHandler,), {"proxy_handler": self.handler}
        )
        return ThreadingHTTPServer((self._host, self._port), handler_cls)

    @property
    def url(self) -> str:
        """对外基础 URL（含实际绑定端口，port=0 时可用于发现临时端口）。"""
        port = self._port
        if self._httpd is not None:
            port = self._httpd.server_address[1]
        return f"http://{self._host}:{port}"

    def start(self) -> None:
        """在后台线程启动服务（非阻塞）。"""
        if self._httpd is not None:
            return
        self._httpd = self._make_httpd()
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="spirit-proxy", daemon=True
        )
        self._thread.start()
        logger.info("代理服务器已启动: %s/v1", self.url)

    def serve_forever(self) -> None:
        """阻塞式启动（前台运行，Ctrl-C 退出）。"""
        if self._httpd is None:
            self._httpd = self._make_httpd()
            self._port = self._httpd.server_address[1]
        logger.info("代理服务器前台运行: %s/v1", self.url)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - 交互中断
            logger.info("代理服务器收到中断，停止")
        finally:
            self.stop()

    def stop(self) -> None:
        """停止服务并回收线程（幂等）。"""
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as exc:  # pragma: no cover
                logger.debug("停止代理服务器异常: %s", exc)
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


__all__ = ["ProxyServer", "ProxyHandler", "ProxyResponse"]
