"""跨平台 Computer Use 就绪度 + 权限探测 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/permissions.py``：桌面控制驱动在各平台「可驱动」的含义
不同——

* **macOS**：需显式 TCC 授权（辅助功能 + 屏幕录制）。
* **Windows**：无 TCC 开关；就绪度 == 驱动健康度（首次运行可能触发 SmartScreen）。
* **Linux**：经 X11/XWayland 辅助控制；就绪度 == 驱动健康度。

各平台通用信号是驱动的 ``doctor --json``（二进制完整性 + 平台支持）。
:func:`computer_use_status` 把它与 macOS 权限细节折叠成一个 payload，供桌面卡片 /
CLI / 状态接口消费。

**可测试抽象层**：真实探测要 spawn 第三方驱动二进制，无法在单测里跑。故所有外部副作用
（``shutil.which`` 找二进制、``subprocess.run`` 跑探测）都收敛到**可注入的 seam**
（``which`` / ``runner`` 关键字参数），测试传入假实现即可离线断言各平台 / 各故障分支的
就绪度折叠逻辑，无需真实驱动。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

# 有桌面控制驱动运行时的平台（对齐工具平台门）。
RUNTIME_PLATFORMS = frozenset({"darwin", "win32", "linux"})
# macOS 权限布尔字段。
_BOOLS = ("accessibility", "screen_recording", "screen_recording_capturable")

# seam 类型别名：which(name)->路径|None；runner(cmd列表, timeout)->CompletedProcess 形状。
WhichFn = Callable[[str], Optional[str]]
RunnerFn = Callable[[List[str], float], Any]


def driver_cmd(override: Optional[str] = None) -> str:
    """解析驱动命令名（override → ``SPIRIT_CUA_DRIVER_CMD`` → config → ``cua-driver``）。"""
    if override:
        return override
    env = os.environ.get("SPIRIT_CUA_DRIVER_CMD", "").strip()
    if env:
        return env
    from spirit.config import get_config_value
    return str(get_config_value("computer_use.driver_cmd", "cua-driver") or "cua-driver")


def _child_env() -> Dict[str, str]:
    """驱动子进程环境：剥离 provider API key（第三方二进制绝不应继承密钥）。"""
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper.endswith("_TOKEN") or upper.endswith("_SECRET"):
            env.pop(key, None)
    return env


def _default_runner(cmd: List[str], timeout: float) -> Any:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=_child_env(), stdin=subprocess.DEVNULL,
    )


def _json_out(binary: str, args: List[str], timeout: float, runner: RunnerFn) -> Any:
    """跑 ``binary args`` 并把 stdout 解析为 JSON；任何失败返回 None。"""
    try:
        raw = (runner([binary, *args], timeout).stdout or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _doctor(binary: str, runner: RunnerFn) -> Optional[Dict[str, Any]]:
    """``doctor --json`` → ``{ok, checks:[{label,status,message}]}``。"""
    data = _json_out(binary, ["doctor", "--json"], 12, runner)
    if not isinstance(data, dict):
        return None
    checks: List[Dict[str, str]] = [
        {
            "label": str(p.get("label", "")),
            "status": str(p.get("status", "")),
            "message": str(p.get("message", "")),
        }
        for p in data.get("probes", [])
        if isinstance(p, dict)
    ]
    return {"ok": bool(data.get("ok")), "checks": checks}


def _mac_permissions(binary: str, out: Dict[str, Any], runner: RunnerFn) -> None:
    """把 ``permissions status --json`` 的布尔字段折叠进 ``out``。"""
    data = _json_out(binary, ["permissions", "status", "--json"], 10, runner)
    if isinstance(data, dict):
        out.update({k: data[k] for k in _BOOLS if isinstance(data.get(k), bool)})
        if isinstance(data.get("source"), dict):
            out["source"] = data["source"]
    else:
        out["error"] = "cua-driver permissions status 探测失败或返回非法 JSON"


def computer_use_status(
    driver_cmd_override: Optional[str] = None,
    *,
    platform: Optional[str] = None,
    which: Optional[WhichFn] = None,
    runner: Optional[RunnerFn] = None,
) -> Dict[str, Any]:
    """统一的、OS 感知的 Computer Use 就绪度（供桌面卡片 / CLI / 状态接口）。

    ``ready`` 是 UI 唯一关心的信号：macOS 上是两个 TCC 授权都为真；其它平台是驱动健康度
    （无 TCC 模型）。``None`` 表示未知（二进制缺失 / 探测失败）。``can_grant`` 仅 macOS 为真。

    Args:
        driver_cmd_override: 驱动命令名覆盖（缺省经 :func:`driver_cmd` 解析）。
        platform: 平台覆盖（缺省 ``sys.platform``）——测试注入点。
        which: ``shutil.which`` 替换——测试注入点。
        runner: ``subprocess.run`` 替换（``(cmd, timeout) -> 带 .stdout 的对象``）——测试注入点。
    """
    plat = platform if platform is not None else sys.platform
    which_fn = which if which is not None else shutil.which
    run_fn = runner if runner is not None else _default_runner

    binary = which_fn(driver_cmd(driver_cmd_override))
    out: Dict[str, Any] = {
        "platform": plat,
        "platform_supported": plat in RUNTIME_PLATFORMS,
        "installed": bool(binary),
        "version": None,
        "ready": None,
        "can_grant": plat == "darwin",
        "checks": [],
        "source": None,
        "error": None,
        **{k: None for k in _BOOLS},
    }
    if not binary:
        return out

    try:
        out["version"] = (run_fn([binary, "--version"], 5).stdout or "").strip() or None
    except Exception:
        pass

    doctor = _doctor(binary, run_fn)
    if doctor is not None:
        out["checks"] = doctor["checks"]

    if plat == "darwin":
        _mac_permissions(binary, out, run_fn)
        if out["error"] is None:
            out["ready"] = out["accessibility"] is True and out["screen_recording"] is True
    elif doctor is not None:
        # macOS 之外无 TCC 模型——就绪度即驱动健康度。
        out["ready"] = doctor["ok"]
    return out


def request_permissions_grant(
    driver_cmd_override: Optional[str] = None,
    *,
    platform: Optional[str] = None,
    which: Optional[WhichFn] = None,
    runner: Optional[RunnerFn] = None,
) -> int:
    """运行 ``permissions grant``（macOS）；返回驱动退出码。

    返回：0 成功；2 二进制缺失；64 非 macOS 平台（无 TCC 权限模型可授予）。
    外部副作用同样经 ``which`` / ``runner`` seam 注入，测试可离线驱动各分支。
    """
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return 64

    which_fn = which if which is not None else shutil.which
    run_fn = runner if runner is not None else _default_runner
    binary = which_fn(driver_cmd(driver_cmd_override))
    if not binary:
        return 2

    try:
        return int(run_fn([binary, "permissions", "grant"], 120).returncode)
    except KeyboardInterrupt:  # pragma: no cover - 交互式
        return 130
    except Exception:  # pragma: no cover - 防御性
        return 2


__all__ = [
    "RUNTIME_PLATFORMS",
    "driver_cmd",
    "computer_use_status",
    "request_permissions_grant",
]
