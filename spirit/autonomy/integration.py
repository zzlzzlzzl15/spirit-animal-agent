"""Autonomy 部署集成 —— Spirit Agent Phase 7.D（把常驻循环接入桌宠）。

把 :class:`~spirit.autonomy.explorer.AutonomyLoop` + :class:`~spirit.autonomy.bridge.DesktopBridge`
组装并挂到桌面宠物的 :class:`~spirit.desktop.ws_server.WSServer`，实现"桌宠内置常驻"形态：

- **HITL 通道**：``HumanInTheLoop(ask=bridge.ask)`` —— 该问就问（桌宠气泡广播），
  没回答 fail-open 自主决策（铁律）；
- **探索大脑**：``caller`` 从配置经 Transport 工厂构造纯补全 seam（无 api_key/异常 → None，
  循环退化为空转，绝不崩溃）；
- **持久记忆**：``EvolutionMemory`` 跨轮沉淀轨迹/洞察，让 Curriculum 复盘有据、越探越聪明；
- **DRS 三角色**：Actor/Verifier/Curriculum 用同一 ``caller`` 接线，让深挖闭环真正推理
  （无 caller → None，fail-soft 退化为安全默认，绝不误判成功）；
- **真实执行**：``execute`` seam 驱动完整 :class:`~spirit.agent.agent.SpiritAgent` 动手做任务，
  工作目录锁定沙箱根 + 注入 workspace 系统提示（绝不触碰主代码库，危险命令仍走审批）；
- **常驻线程**：``AutonomyLoop.start()`` 守护线程周期 tick；
- **WS 命令**：``bridge.attach(server)`` 注册 autonomy_answer/status/start/stop。

整段集成 fail-soft：任何一步失败只记日志、降级为空转/无通道，不影响桌宠主服务。
离线可测：``build_autonomy(server=None, enable_agent_execute=False)`` 不依赖事件循环与网络。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from spirit.autonomy.bridge import DesktopBridge, bridge_from_ws
from spirit.autonomy.explorer import AutonomyLoop, DEFAULT_INTERVAL_SECONDS
from spirit.autonomy.sandbox import Sandbox
from spirit.evolution.curriculum import CurriculumPlanner
from spirit.evolution.hitl import HumanInTheLoop, resolve_mode
from spirit.evolution.loop import STOP_CURRICULUM_REVIEW, STOP_VERIFIER_PASS
from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.roles import ActorRole, LLMCaller
from spirit.evolution.verifier import Verifier

logger = logging.getLogger(__name__)

# execute 期间把工作目录切到沙箱根；用可重入锁串行化，避免多次 execute 竞态 chdir。
_EXECUTE_LOCK = threading.RLock()

# 自主探索模式下注入给 SpiritAgent 的 workspace 系统提示（限定可写边界）。
_WORKSPACE_PROMPT = (
    "你正处于【自主探索模式】，工作目录已被锁定在沙箱：{workspace}\n"
    "铁律（不可违反）：\n"
    "- 只允许在上述沙箱目录内创建/修改文件；绝不触碰沙箱之外的主代码库或用户文件。\n"
    "- 写文件请使用相对路径（相对当前工作目录）或上述沙箱绝对路径。\n"
    "- 危险命令仍需人工审批；读主代码库允许（探索需要），但写永远只能在沙箱内。\n\n"
    "当前探索任务：{task}\n"
    "请动手完成它，最后用一段话总结：你做了什么、产物在哪、结论如何。"
)

# 常驻自主探索默认间隔：5 小时（对齐常见 API token/配额每 5 小时刷新的窗口）。
DEFAULT_RESIDENT_INTERVAL_SECONDS = 5 * 3600.0

# 产物快照：execute 跑完后回读沙箱内新建/改动的小文本文件，作为 Verifier 的客观证据
# （修复 STALLED：此前 Evidence 只有工具调用名，Verifier 无据可裁 → 连续 UNVERIFIED）。
_PRODUCT_TEXT_EXTS = {".md", ".json", ".csv", ".py", ".txt", ".log",
                      ".yaml", ".yml", ".rst", ".toml"}
_PRODUCT_MAX_FILES = 12        # 最多回读文件数
_PRODUCT_MAX_BYTES = 8000      # 单文件最大回读字节
_PRODUCT_MAX_TOTAL = 40000     # 全部文件累计最大字符（防证据爆炸）


def resolve_interval_seconds(explicit: Optional[float] = None) -> float:
    """解析常驻探索间隔（秒）。

    优先级：显式传入 > 配置 ``autonomy.interval_hours`` > ``autonomy.interval_seconds``
    > 默认 5 小时（对齐 API token 每 5 小时刷新的配额窗口，每个窗口跑一轮“接着上次记忆继续”的探索）。
    """
    if explicit is not None:
        return float(explicit)
    try:
        from spirit.config import get_config_value
        hours = get_config_value("autonomy.interval_hours", None)
        if hours:
            return float(hours) * 3600.0
        secs = get_config_value("autonomy.interval_seconds", None)
        if secs:
            return float(secs)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("autonomy.integration: 读 autonomy.interval 配置失败，用默认: %s", exc)
    return DEFAULT_RESIDENT_INTERVAL_SECONDS


def _autonomy_cfg() -> Dict[str, Any]:
    """读取配置里的 ``autonomy`` 段（经 load_config 合并 YAML）；不可用 → {}（fail-soft）。"""
    try:
        from spirit.config import load_config
        return (load_config() or {}).get("autonomy", {}) or {}
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("autonomy.integration: 读 autonomy 配置失败: %s", exc)
        return {}


def resolve_domain(explicit: Optional[str] = None) -> str:
    """解析探索领域：显式传入 > 配置 ``autonomy.domain`` > ""（不限定领域）。"""
    if explicit:
        return str(explicit)
    return str(_autonomy_cfg().get("domain", "") or "")


def resolve_run_on_start(explicit: Optional[bool] = None) -> bool:
    """解析“启动即跑首轮”：显式传入 > 配置 ``autonomy.run_on_start`` > False。

    为 True 时，:meth:`AutonomyLoop.start` 会在守护线程里先立即跑一轮探索（而非等满一个间隔）。
    """
    if explicit is not None:
        return bool(explicit)
    return bool(_autonomy_cfg().get("run_on_start", False))


def resolve_stop_policy(explicit: Optional[str] = None) -> str:
    """解析停止策略：显式传入 > 配置 ``autonomy.stop_policy`` > ``curriculum_review``。

    缺省 ``curriculum_review``（PASS 后 Curriculum 仍可要求再练/再改）——让 Agent 对任务
    **自动迭代打磨**直至真收敛；若要省 token 可配 ``verifier_pass``（一经验证即停）。
    """
    if explicit:
        return str(explicit)
    val = str(_autonomy_cfg().get("stop_policy", "") or "").strip()
    return val or STOP_CURRICULUM_REVIEW


def resolve_max_rounds(explicit: Optional[int] = None) -> int:
    """解析单任务最大迭代轮次：显式传入 > 配置 ``autonomy.max_rounds`` > 5。"""
    if explicit is not None:
        return max(1, int(explicit))
    val = _autonomy_cfg().get("max_rounds", 0)
    try:
        return max(1, int(val)) if val else 5
    except (TypeError, ValueError):
        return 5


def make_llm_caller() -> Optional[LLMCaller]:
    """从配置构造纯补全 caller；无 api_key / 任何异常 → None（fail-soft 空转）。"""
    try:
        from spirit.config import load_config
        from spirit.agent.transports.base import TransportConfig
        from spirit.agent.transports.factory import create_transport

        llm = (load_config() or {}).get("llm", {}) or {}
        if not llm.get("api_key"):
            logger.debug("autonomy.integration: 无 api_key → caller=None（空转）")
            return None
        cfg = TransportConfig(
            provider=str(llm.get("provider") or "openai"),
            api_key=str(llm.get("api_key") or ""),
            base_url=str(llm.get("base_url") or ""),
            model=str(llm.get("model") or "gpt-4o"),
        )
        transport = create_transport(cfg)

        def caller(messages, temperature=0.7, max_tokens=300, timeout=30.0):
            resp = transport.chat(
                list(messages), tools=None,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            )
            for attr in ("content", "text", "message"):
                value = getattr(resp, attr, None)
                if isinstance(value, str) and value.strip():
                    return value
            return str(resp or "")

        return caller
    except Exception as exc:
        logger.warning("autonomy.integration: 构造 caller 失败 → None: %s", exc)
        return None


def make_memory() -> Optional[EvolutionMemory]:
    """构造跨轮持久化记忆（``<SPIRIT_HOME>/evolution_memory``）。异常 → None（fail-soft）。"""
    try:
        return EvolutionMemory()
    except Exception as exc:
        logger.warning("autonomy.integration: 构造记忆失败 → None: %s", exc)
        return None


def _sandbox_snapshot(sandbox: Sandbox) -> Dict[str, Tuple[float, int]]:
    """快照沙箱内所有文件的 ``(mtime, size)``，用于 diff 出本轮新建/改动的产物。"""
    snap: Dict[str, Tuple[float, int]] = {}
    try:
        root = Path(sandbox.root)
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    st = path.stat()
                    snap[path.relative_to(root).as_posix()] = (st.st_mtime, st.st_size)
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover - 快照失败不阻断执行
        logger.debug("autonomy.integration: 沙箱快照失败: %s", exc)
    return snap


def _collect_products(
    sandbox: Sandbox, before: Dict[str, Tuple[float, int]]
) -> Tuple[List[str], Dict[str, str]]:
    """对比执行前后快照，返回（本轮新建/改动文件列表, 小文本产物内容字典）。

    只回读小体积文本文件（.md/.json/.csv/.py 等），并限制文件数/单文件/累计字节，
    避免把二进制或超大文件塞进 Evidence。读失败的文件跳过（fail-soft）。
    """
    after = _sandbox_snapshot(sandbox)
    root = Path(sandbox.root)
    changed: List[str] = []
    files: Dict[str, str] = {}
    total = 0
    for rel, (mtime, size) in after.items():
        prev = before.get(rel)
        if prev is not None and prev[0] >= mtime and prev[1] == size:
            continue  # 未改动
        changed.append(rel)
        ext = os.path.splitext(rel)[1].lower()
        if (ext in _PRODUCT_TEXT_EXTS and size <= _PRODUCT_MAX_BYTES
                and len(files) < _PRODUCT_MAX_FILES and total < _PRODUCT_MAX_TOTAL):
            try:
                content = (root / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            files[rel] = content
            total += len(content)
    return changed, files


def make_execute(
    sandbox: Sandbox,
    *,
    agent_factory: Optional[Callable[[Any], Any]] = None,
    max_iterations: int = 40,
) -> Optional[Callable[[str], Any]]:
    """构造 ``execute`` seam：用完整 SpiritAgent 在沙箱工作目录内真实执行探索任务。

    - 全能力档：Agent 可读写文件/跑命令，但 ``os.chdir`` 锁定到 ``sandbox.root``，
      并注入 :data:`_WORKSPACE_PROMPT` 约束写操作只在沙箱内（危险命令仍走 Agent 审批）；
    - 返回 :class:`~spirit.evolution.loop.AttemptResult`（observation=Agent 回复，
      evidence=工具调用日志 + 产物），供 Verifier 独立校验；
    - 无 api_key / 任何异常 → None（fail-soft，循环退化为“提议+写笔记”）。

    ``agent_factory`` 可注入（离线测试用）；缺省构造真实 :class:`SpiritAgent`。
    """
    try:
        from spirit.config import load_config
        cfg_dict = load_config() or {}
        llm = cfg_dict.get("llm", {}) or {}
        if not llm.get("api_key"):
            logger.debug("autonomy.integration: 无 api_key → execute=None")
            return None

        def execute(task: str) -> Any:
            from spirit.agent.agent import AgentConfig, SpiritAgent
            from spirit.evolution.loop import AttemptResult
            from spirit.evolution.verifier import Evidence

            text = str(task or "").strip()
            workspace = Path(sandbox.root)
            workspace.mkdir(parents=True, exist_ok=True)
            before = _sandbox_snapshot(sandbox)   # 执行前快照，用于 diff 出本轮产物
            config = AgentConfig.from_dict(cfg_dict)
            config.max_iterations = int(max_iterations)
            config.system_prompt = _WORKSPACE_PROMPT.format(workspace=workspace, task=text)
            agent = agent_factory(config) if agent_factory else SpiritAgent(config)

            prev_cwd = os.getcwd()
            with _EXECUTE_LOCK:
                try:
                    os.chdir(workspace)   # 锁定工作目录→相对路径写入落在沙箱
                    result = agent.chat(text) or {}
                except Exception as exc:
                    logger.warning("autonomy.integration: execute 执行异常: %s", exc)
                    result = {"response": f"（执行异常：{exc}）"}
                finally:
                    try:
                        os.chdir(prev_cwd)   # 无论成败都还原工作目录
                    except Exception:  # pragma: no cover - 还原失败不阻断
                        pass

            response = str(result.get("response") or "")
            tool_calls = result.get("tool_calls") or []
            log_lines = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    name = tc.get("name") or tc.get("function") or "?"
                else:
                    name = str(tc)
                log_lines.append(f"- 调用工具: {name}")
            # 快照本轮沙箱产物（新建/改动的小文本文件）→ Verifier 的客观证据（修复 STALLED）
            changed, files = _collect_products(sandbox, before)
            evidence = Evidence(
                task=text,
                logs="\n".join(log_lines),
                files=files,
                artifacts={"response": response} if response else {},
                env_state={
                    "iterations": result.get("iterations", 0),
                    "sandbox_files_changed": changed,
                    "sandbox_file_count": len(changed),
                },
            )
            return AttemptResult(observation=response, evidence=evidence)

        return execute
    except Exception as exc:
        logger.warning("autonomy.integration: 构造 execute 失败 → None: %s", exc)
        return None


def make_proposer(
    caller: Optional[LLMCaller],
    *,
    domain: str = "",
    limit: int = 3,
) -> Optional[Callable[[Any], List[str]]]:
    """构造“架构型自提议” propose seam：每轮基于沉淀记忆提出下一个架构/设计型问题。

    “做完一个→自提新架构问题→继续探索”的关键：把 ``progress_summary(memory)`` 喂给 LLM，
    让它接着上次的轨迹/洞察递进提出新的【架构/设计型】问题（而非从零开始）。

    - 有 caller → 返回 propose 函数（供 :class:`AutonomyLoop` 每轮调用）；
    - 无 caller → None（fail-soft，AutonomyLoop 回退到 _default_propose 或空转）。

    ``domain`` 可限定探索领域（如“金融”）；``limit`` 控制每轮候选数（>1 触发 HITL 分叉询问）。
    """
    if caller is None:
        return None

    def propose(memory: Any) -> List[str]:
        try:
            summary = CurriculumPlanner(memory=memory).progress_summary(memory)
        except Exception:  # pragma: no cover - 记忆读取失败降级
            summary = "（无历史记忆）"
        domain_hint = f"聚焦领域：{domain}。\n" if domain else ""
        prompt = (
            "你是自主探索规划器。基于下方已沉淀的记忆，提出接下来最值得探索的 "
            f"{limit} 个【架构/设计型】问题（例如：系统结构权衡、模块边界划分、设计模式选型、"
            "可扩展性/可靠性方案、算法或数据结构的架构级取舍、接口契约设计）。\n"
            f"{domain_hint}"
            "要求：每个问题都能在当前沙箱目录内通过写代码/文档/分析来动手验证，不依赖外部实时数据；"
            "问题之间尽量递进（接着上次的探索继续深入），避免重复已做过的。\n"
            "每行一个，不要编号、不要解释。\n"
            "当前记忆摘要：\n" + (summary or "（无历史记忆）")
        )
        try:
            raw = caller([{"role": "user", "content": prompt}], 0.7, 400, 40.0)
        except Exception as exc:
            logger.warning("autonomy.integration: 架构提议调用失败 → 空候选: %s", exc)
            return []
        lines = [
            ln.strip(" -·*\t0123456789.)").strip()
            for ln in str(raw or "").splitlines()
        ]
        return [ln for ln in lines if ln][:limit]

    return propose


def build_autonomy(
    *,
    server: Any = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    caller: Optional[LLMCaller] = None,
    memory: Any = None,
    propose: Optional[Callable[[Any], List[str]]] = None,
    execute: Optional[Callable[[str], Any]] = None,
    sandbox: Optional[Sandbox] = None,
    interval_seconds: Optional[float] = None,
    hitl_timeout: float = 300.0,
    enable_agent_execute: bool = True,
    max_iterations: int = 40,
    stop_policy: Optional[str] = None,
    max_rounds: Optional[int] = None,
    domain: str = "",
) -> Tuple[DesktopBridge, AutonomyLoop]:
    """组装桥接 + 常驻循环（不启动线程、不注册命令）。离线可测（server=None）。

    将“完全自动化”所需的四块全部接上：持久记忆 + DRS 三角色（用 caller）+
    真实 execute（除非 ``enable_agent_execute=False`` 或已注入 ``execute``）。
    """
    if server is not None:
        bridge = bridge_from_ws(server, loop, timeout=hitl_timeout)
    else:
        bridge = DesktopBridge(timeout=hitl_timeout)

    sandbox = sandbox or Sandbox()
    if memory is None:
        memory = make_memory()

    hitl = HumanInTheLoop(mode=resolve_mode(), ask=bridge.ask, memory=memory,
                          timeout=hitl_timeout)

    # DRS 三角色：有 caller 时用同一 caller 接线（让闭环真正推理）；无 caller → None
    # （AutonomyLoop/EvolutionLoop 会 fail-soft 退化为安全默认：Verifier→UNVERIFIED、
    # Curriculum→STALLED，绝不误判成功）。
    actor = ActorRole(caller) if caller else None
    verifier = Verifier(caller=caller) if caller else None
    curriculum = CurriculumPlanner(caller=caller, memory=memory) if caller else None

    # 真实执行 seam：全能力 + 沙箱工作目录约束（无 api_key → None，循环仅提议+写笔记）。
    if execute is None and enable_agent_execute:
        execute = make_execute(sandbox, max_iterations=max_iterations)

    # 架构型自提议：有 caller 且未注入 propose 时接上（每轮基于记忆提出下一个架构问题，
    # 实现“做完一个→自提新架构问题→继续探索”）。
    if propose is None and caller is not None:
        propose = make_proposer(caller, domain=domain)

    autonomy = AutonomyLoop(
        caller=caller, memory=memory, hitl=hitl, sandbox=sandbox,
        propose=propose, execute=execute, actor=actor, verifier=verifier,
        curriculum=curriculum,
        interval_seconds=resolve_interval_seconds(interval_seconds),
        stop_policy=resolve_stop_policy(stop_policy),
        max_rounds=resolve_max_rounds(max_rounds),
    )
    bridge.controller = autonomy
    return bridge, autonomy


def start_autonomy(
    server: Any,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    caller: Optional[LLMCaller] = None,
    interval_seconds: Optional[float] = None,
    autostart: bool = True,
    enable_agent_execute: bool = True,
    max_iterations: int = 40,
    stop_policy: Optional[str] = None,
    max_rounds: Optional[int] = None,
    domain: str = "",
) -> Optional[AutonomyLoop]:
    """把常驻自主循环挂到桌宠 WS 服务并（可选）启动守护线程。失败 → None（fail-soft）。

    缺省即“完全自动化”：持久记忆 + DRS 三角色推理 + 真实 execute（有 api_key 时）全部接上；
    探索间隔缺省 5 小时（对齐 API token 刷新），可经 ``autonomy.interval_hours`` 配置覆盖。
    """
    try:
        bridge, autonomy = build_autonomy(
            server=server, loop=loop, caller=caller,
            interval_seconds=interval_seconds,
            enable_agent_execute=enable_agent_execute, max_iterations=max_iterations,
            stop_policy=stop_policy, max_rounds=max_rounds, domain=domain,
        )
        bridge.attach(server)
        if autostart:
            autonomy.start()
        logger.info("autonomy.integration: 常驻自主循环已挂载（autostart=%s, 间隔=%.0fs, execute=%s）",
                    autostart, autonomy.interval_seconds,
                    autonomy._evolution._execute is not None)
        return autonomy
    except Exception as exc:
        logger.warning("autonomy.integration: 挂载自主循环失败（桌宠继续运行）: %s", exc)
        return None


__all__ = ["make_llm_caller", "make_memory", "make_execute", "make_proposer",
           "resolve_interval_seconds", "DEFAULT_RESIDENT_INTERVAL_SECONDS",
           "resolve_stop_policy", "resolve_max_rounds",
           "build_autonomy", "start_autonomy"]
