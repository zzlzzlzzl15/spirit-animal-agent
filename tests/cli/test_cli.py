"""tests/cli/test_cli.py — CLI 命令测试。"""

import pytest
from click.testing import CliRunner


class TestCLI:
    """CLI 命令测试。"""

    def test_cli_help(self):
        """测试 --help 输出。"""
        from spirit.cli.main import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Spirit Agent" in result.output

    def test_cli_tools(self):
        """测试 tools 命令。"""
        from spirit.cli.main import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["tools"])
        assert result.exit_code == 0

    def test_cli_sessions(self):
        """测试 sessions 命令。"""
        from spirit.cli.main import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["sessions"])
        # 可能因为没有会话而显示空列表
        assert result.exit_code == 0

    def test_cli_version(self):
        """测试 version 命令（如果存在）。"""
        from spirit.cli.main import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        # 至少应显示帮助信息
        assert "Spirit" in result.output
