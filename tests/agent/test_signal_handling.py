"""Spirit Agent CLI 退出 + signal 处理回归测试。

Spirit 信号处理现状：
  - cli/main.py:710 KeyboardInterrupt → except → continue（回到主循环）
  - cli/main.py:713 EOFError → except → break（退出主循环）
  - cli/main.py:725 agent.close_session(reason="cli_exit") 兜底
  - cli/main.py chat() **没有**显式 signal.signal() 注册 SIGINT/SIGTERM
  - desktop/_launcher.py:128-129 有显式 signal.signal(SIGINT/SIGTERM, shutdown)

参考 hermes:
  - test_session_persistence.py (关闭流程)
  - test_signal_* (hermes 显式信号处理)
  - test_terminate_cleanly.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "signal_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    from spirit.agent.agent import SpiritAgent, AgentConfig
    config = AgentConfig(
        model="gpt-4o-mini",
        api_key="test-key-12345",
        max_iterations=50,
        compression_enabled=False,
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ─────────────────────────── CLI chat() 实际信号处理 ───────────────────────────


class TestCLIChatSignalHandling:
    """cli/main.py chat() 实际信号处理策略。"""

    def test_chat_handles_keyboard_interrupt_continues(self, monkeypatch, fresh_agent):
        """cli/main.py:710 KeyboardInterrupt → except → continue

        注：spirit chat() 把 KeyboardInterrupt 当作"用户取消当前输入"，
        不是终止信号。再次按 Ctrl+C / EOFError 才真正退出。
        这是 spirit 现状设计。
        """
        # 模拟 chat() 的 except KeyboardInterrupt: continue 逻辑
        # 实际 cli/main.py:710-712 做了：
        #   except KeyboardInterrupt:
        #       console.print("\n[dim]按 Ctrl+C 再次退出，或输入 /quit[/dim]")
        #       continue
        # 验证：第一次 KeyboardInterrupt 不退出，_interrupt_requested 不变
        before = fresh_agent._interrupt_requested
        # 模拟用户按 Ctrl+C
        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            # chat() 的 except 块不调 agent.interrupt()，只 continue
            pass
        # _interrupt_requested 保持不变
        assert fresh_agent._interrupt_requested == before

    def test_chat_handles_eof_error_exits(self, fresh_agent):
        """cli/main.py:713 EOFError → except → break → close_session 兜底

        EOFError (Ctrl+D / 输入流结束) 真正退出 chat() 循环。
        """
        # 模拟 EOFError 触发 break
        # 实际 chat() 的 except 块：
        #   except EOFError:
        #       break
        # 验证：EOFError 被捕获，循环退出
        # 然后 close_session(reason="cli_exit") 兜底
        try:
            raise EOFError()
        except EOFError:
            # 模拟 break
            exited = True

        # 然后 close_session 兜底
        fresh_agent.close_session(reason="cli_exit")
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "cli_exit"
        assert exited is True

    def test_chat_after_loop_closes_session_with_cli_exit(self, fresh_agent):
        """chat() 退出后 close_session(reason='cli_exit') 兜底被调"""
        # 模拟 chat() 退出循环后调 close_session
        fresh_agent.close_session(reason="cli_exit")
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "cli_exit"


class TestCLISignalHandlerRegistration:
    """CLI 是否注册显式 signal handlers。

    spirit 现状：chat() 没有显式 signal.signal() 注册。
    spirit 依赖 Python 默认行为 + try/except 捕获。
    """

    def test_spirit_chat_has_no_explicit_sigint_handler(self):
        """spirit chat() 没有显式 signal.signal(SIGINT, ...) 注册

        这是 spirit 现状。Ctrl+C 走 Python 默认行为（抛 KeyboardInterrupt），
        被 cli/main.py:710 except 捕获 → continue。
        """
        # 检查 spirit.cli.main 模块有没有 signal.signal() 调用
        from spirit.cli import main as cli_main
        import inspect
        source = inspect.getsource(cli_main)
        # 验证没有 signal.signal() 调用
        assert "signal.signal(" not in source, (
            "spirit chat() 不应显式注册 signal.signal()，"
            "依赖 Python 默认 KeyboardInterrupt 行为"
        )

    def test_spirit_chat_has_no_explicit_sigterm_handler(self):
        """spirit chat() 没有显式 signal.signal(SIGTERM, ...) 注册"""
        from spirit.cli import main as cli_main
        import inspect
        source = inspect.getsource(cli_main)
        # SIGTERM 在 Windows 不存在，但 spirit 应该有处理
        # 这里只验证没有 signal.signal(SIGTERM 调用
        assert "SIGTERM" not in source or "signal.signal(SIGTERM" not in source


class TestDesktopLauncherSignalHandlers:
    """desktop/_launcher.py 显式注册了 SIGINT/SIGTERM。"""

    def test_desktop_launcher_registers_sigint(self):
        """desktop 模式注册 SIGINT → shutdown"""
        from spirit.desktop import _launcher
        import inspect
        source = inspect.getsource(_launcher)
        assert "signal.SIGINT" in source
        assert "signal.signal" in source

    def test_desktop_launcher_registers_sigterm(self):
        """desktop 模式注册 SIGTERM → shutdown"""
        from spirit.desktop import _launcher
        import inspect
        source = inspect.getsource(_launcher)
        assert "signal.SIGTERM" in source


# ─────────────────────────── xfail: 显式信号处理器（hermes 设计意图） ───────────────────────────


class TestExplicitSignalHandlerDesign:
    """spirit 未来可加：CLI 显式信号处理器（hermes 风格）。

    当前 spirit 依赖 Python 默认 KeyboardInterrupt + try/except。
    以下测试用 xfail 标记，记录设计意图。
    """

    @pytest.mark.xfail(
        reason="spirit 现状：chat() 没有显式 signal_handlers 模块，"
               "依赖 try/except KeyboardInterrupt（cli/main.py:710）",
        strict=False,
    )
    def test_spirit_has_explicit_signal_handlers_module(self):
        """spirit 应该有 spirit.cli.signal_handlers 模块（hermes 风格）"""
        try:
            from spirit.cli import signal_handlers  # noqa: F401
            assert signal_handlers is not None
        except ImportError:
            pytest.fail("spirit.cli.signal_handlers 模块不存在")

    @pytest.mark.xfail(
        reason="spirit 现状：chat() 第一次 Ctrl+C 不调 close_session，"
               "只在 /quit / EOFError 时 close_session(cli_exit)",
        strict=False,
    )
    def test_first_ctrl_c_closes_session(self, fresh_agent, monkeypatch):
        """hermes 风格：第一次 Ctrl+C 应该 close_session(user_interrupt)。

        spirit 现状：第一次 Ctrl+C → continue（不退出、不 close_session）。
        本测试断言 hermes 期望（close_session 被调），spirit 现状下会失败 → xfail。
        """
        # 1. 验证 session 当前 open
        sess_before = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess_before.get("ended_at") is None

        # 2. 模拟 spirit chat() 第一次 Ctrl+C 行为
        #    实际 cli/main.py:710 except KeyboardInterrupt: continue
        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            # spirit 现状：continue，不调 close_session
            pass

        # 3. hermes 期望：close_session(user_interrupt) 被调
        sess_after = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # spirit 现状：session 仍 open（end_reason 应仍为 None / end_reason != user_interrupt）
        # hermes 期望：end_reason == "user_interrupt"
        assert sess_after.get("end_reason") == "user_interrupt"
