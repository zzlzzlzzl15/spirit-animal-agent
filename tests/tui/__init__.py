"""tests/tui — Spirit TUI 网关精简子集测试套件（Phase 4.4）。

对标 Hermes ``tests/tui_gateway/``（test_project_tree / test_render / test_protocol）+
``tests/test_slash_worker_watchdog.py``。全部离线：git 解析、渲染器、斜杠 runner、时钟、传输
均经 seam 注入，无需真实 WebSocket / 子进程 / 图形会话。
"""
