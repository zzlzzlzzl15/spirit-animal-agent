"""代码执行工具 — execute_code（Python 沙箱）。

参考 Hermes 的 tools/code_execution_tool.py 设计，简化版：
- 在子进程中执行 Python 代码
- 捕获 stdout/stderr
- 超时控制
- 安全限制
"""

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = get_config_value("timeouts.execute_code_default", 60)
MAX_TIMEOUT = get_config_value("timeouts.execute_code_max", 300)
MAX_OUTPUT_BYTES = get_config_value("limits.execute_code_max_bytes", 50_000)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

EXECUTE_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_code",
        "description": (
            "在沙箱中执行 Python 代码。\n\n"
            "代码在独立子进程中运行，捕获 stdout 和 stderr。\n"
            "适合数据分析、计算验证、快速原型等。\n\n"
            "注意：不能访问 Agent 内部工具，只能使用 Python 标准库和已安装的包。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数（默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}）",
                },
                "working_dir": {
                    "type": "string",
                    "description": "工作目录（默认临时目录）",
                },
            },
            "required": ["code"],
        },
    },
}


def _execute_code_impl(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    working_dir: str = None,
) -> str:
    """执行 Python 代码。"""
    timeout = min(max(timeout, 5), MAX_TIMEOUT)

    # 创建临时文件
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(code)
        script_path = f.name

    work_dir = Path(working_dir) if working_dir else Path(tempfile.gettempdir())

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            timeout=timeout,
            cwd=str(work_dir),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        # 截断大输出
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES] + f"\n\n... [输出截断，共 {len(result.stdout)} 字节]"
        if len(stderr) > MAX_OUTPUT_BYTES // 5:
            stderr = stderr[:MAX_OUTPUT_BYTES // 5] + "\n\n... [stderr 截断]"

        output = {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "success": result.returncode == 0,
        }

        return json.dumps(output, ensure_ascii=False, indent=2)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "error": f"执行超时（{timeout}秒）",
            "stdout": "",
            "stderr": "TIMEOUT",
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })
    finally:
        # 清理临时文件
        try:
            os.unlink(script_path)
        except OSError:
            pass


registry.register(
    name="execute_code",
    toolset="code",
    schema=EXECUTE_CODE_SCHEMA,
    handler=_execute_code_impl,
    description="执行 Python 代码",
    emoji="⚡",
    max_result_size_chars=MAX_OUTPUT_BYTES,
)
