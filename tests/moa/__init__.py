"""tests/moa — Mixture-of-Agents 子系统测试包（Phase 4.1）。

对标 Hermes 的同款「任务检查」用例：
- ``tests/hermes_cli/test_moa_config.py`` → :mod:`tests.moa.test_config`
- ``tests/run_agent/test_moa_loop_mode.py`` → :mod:`tests.moa.test_reference_view`
  （``_reference_messages`` / ``_attach_reference_guidance`` 纯函数契约）+
  :mod:`tests.moa.test_moa_loop`（facade ``create()`` 行为契约）+
  :mod:`tests.moa.test_trace`（完整轮次追踪持久化）
- ``tests/run_agent/test_moa_streaming.py`` → :mod:`tests.moa.test_moa_loop`（流式分支）
- ``tests/cli/test_moa_command.py`` + ``tests/gateway/test_moa_one_shot_restore.py``
  → :mod:`tests.moa.test_commands`（``/moa`` 命令层 + 一次性恢复）

与 Hermes 的关键差异：Spirit 的 monkeypatch seam 是 ``moa_loop._call_slot`` /
``_call_slot_stream``（而非 ``call_llm``），配置注入走 ``_load_moa_config`` 或真实
``SPIRIT_HOME`` + ``config.yaml``（而非 ``HERMES_HOME``）。假响应是 ``TransportResponse``
形状（``.content`` / ``.usage`` dict / ``.tool_calls``）。
"""
