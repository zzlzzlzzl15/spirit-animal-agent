"""Computer Use 可测试抽象层测试套件（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use*.py`` 的 Spirit 适配子集：只覆盖**抽象层**
逻辑（backend 形状 / noop 后端 / schema / 安全分级 / 审批门 / 派发 / 响应塑形 / 就绪度
探测 / 视觉路由），不覆盖 cua-driver 专属集成（Spirit 不实现真实驱动，经 set_backend_factory
注入）。所有测试对着 :class:`~spirit.computer_use.noop_backend.NoopBackend` 断言，无需真实
桌面驱动或图形会话，可在无头 CI 离线运行。
"""
