"""Verifier 独立校验闭环 —— Spirit Agent Phase 6.B（docs/13 §Phase B / §6.2）。

进化的安全带：**反思必须接外部验证器**，否则纯自我反思会把对的改错
（"LLMs Cannot Self-Correct Reasoning Yet", ICLR'24）。本模块实现四件事：

- **B1 独立上下文**：:class:`Verifier` 只看 :class:`Evidence`（客观证据），
  **结构性地屏蔽** Actor 的私有推理与记忆——``Evidence`` 根本没有承载私有推理的字段。
- **B2 证据采集**：:class:`Evidence` 归拢命令退出码 / 日志 / 文件快照 / 产物 /
  环境状态 / 领域客观指标，作为 candidate evidence。
- **B3 接地裁决**：输出 PASS / FAIL / UNVERIFIED + **非空理由**。
- **B4 隔离回滚**：试探性探针（``probe``）对环境的改动，校验后经
  :mod:`spirit.checkpoint` 快照回滚（复用而非重写）。
- **B5 领域验证器接口**：:class:`DomainVerifier` 协议 + 若干确定性验证器
  （文件内容 / 退出码 / 正则 / 指标阈值）。**客观信号 > 模型自评**：先跑客观验证器，
  无法客观裁决时才回退到 LLM（:class:`~spirit.evolution.roles.VerifierRole`）。

铁律（docs/13 §2.3 / §6.2）：基础设施异常 → ``UNVERIFIED``，**绝不当 PASS/FAIL**。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from spirit.evolution.protocol import TargetVerdict, Verdict
from spirit.evolution.roles import LLMCaller, VerifierRole

logger = logging.getLogger(__name__)

# probe 签名：接收 Evidence，可试探性改动环境 / 补充证据；
# 返回 Verdict 表示探针已给出接地裁决，返回 None 表示交给后续验证器。
Probe = Callable[["Evidence"], Optional[Verdict]]


# ---------------------------------------------------------------------------
# B1/B2 · 客观证据容器
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """候选证据（纯客观信号）。

    **隔离保证**：本类不含任何"Actor 私有推理/记忆"字段——Verifier 无从看到，
    从结构上杜绝"自嗨"与作弊（docs/13 §2.1 / §6.2）。
    """

    task: str = ""
    logs: str = ""
    exit_code: Optional[int] = None
    files: Dict[str, str] = field(default_factory=dict)       # path -> 内容快照
    artifacts: Dict[str, str] = field(default_factory=dict)   # name -> 内容
    env_state: Dict[str, Any] = field(default_factory=dict)   # 环境状态键值
    metrics: Dict[str, float] = field(default_factory=dict)   # 领域客观指标（如回测夏普）

    def is_empty(self) -> bool:
        """除 task 外无任何客观信号。"""
        return not (
            self.logs
            or self.exit_code is not None
            or self.files
            or self.artifacts
            or self.env_state
            or self.metrics
        )

    def render(self) -> str:
        """组装成给 Verifier（LLM 或人）看的客观证据文本（只含非空段）。"""
        parts: List[str] = [f"任务要求：\n{self.task or '（未提供）'}"]
        if self.exit_code is not None:
            parts.append(f"命令退出码：{self.exit_code}")
        if self.metrics:
            metric_lines = "\n".join(f"  {k} = {v}" for k, v in self.metrics.items())
            parts.append(f"客观指标：\n{metric_lines}")
        if self.logs:
            parts.append(f"日志：\n{self.logs}")
        if self.files:
            file_blocks = "\n".join(
                f"--- {path} ---\n{content}" for path, content in self.files.items()
            )
            parts.append(f"文件快照：\n{file_blocks}")
        if self.artifacts:
            art_blocks = "\n".join(
                f"--- {name} ---\n{content}" for name, content in self.artifacts.items()
            )
            parts.append(f"产物：\n{art_blocks}")
        if self.env_state:
            env_lines = "\n".join(f"  {k}: {v}" for k, v in self.env_state.items())
            parts.append(f"环境状态：\n{env_lines}")
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "logs": self.logs,
            "exit_code": self.exit_code,
            "files": dict(self.files),
            "artifacts": dict(self.artifacts),
            "env_state": dict(self.env_state),
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Evidence":
        data = data or {}
        exit_code = data.get("exit_code")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None

        def _str_dict(raw: Any) -> Dict[str, str]:
            if not isinstance(raw, dict):
                return {}
            return {str(k): str(v) for k, v in raw.items()}

        def _float_dict(raw: Any) -> Dict[str, float]:
            if not isinstance(raw, dict):
                return {}
            out: Dict[str, float] = {}
            for k, v in raw.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            return out

        return cls(
            task=str(data.get("task", "") or ""),
            logs=str(data.get("logs", "") or ""),
            exit_code=exit_code,
            files=_str_dict(data.get("files")),
            artifacts=_str_dict(data.get("artifacts")),
            env_state=dict(data.get("env_state") or {}),
            metrics=_float_dict(data.get("metrics")),
        )


# ---------------------------------------------------------------------------
# B5 · 领域验证器接口 + 确定性验证器（客观信号 > 模型自评）
# ---------------------------------------------------------------------------

class DomainVerifier(Protocol):
    """领域验证器接口：``verify(task, evidence) -> Optional[Verdict]``。

    返回 ``None`` 表示"无法客观裁决"（相关信号缺失），交给 LLM 回退；
    返回 :class:`Verdict` 表示已接地裁决。金融示例用回测/数据校验，代码用测试。
    """

    name: str

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:  # pragma: no cover
        ...


@dataclass
class ExitCodeVerifier:
    """期望命令退出码（默认 0=成功）。无退出码信号→None（不裁决）。"""

    expected: int = 0
    name: str = "exit_code"

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        if evidence.exit_code is None:
            return None
        ok = evidence.exit_code == self.expected
        return Verdict(
            verdict=TargetVerdict.PASS if ok else TargetVerdict.FAIL,
            reason=f"退出码 {evidence.exit_code}（期望 {self.expected}）",
        )


@dataclass
class FileContentVerifier:
    """期望文件内容包含/等于给定片段。相关文件缺失→None（不裁决）。"""

    expected: Dict[str, str] = field(default_factory=dict)  # path -> 期望内容/子串
    mode: str = "contains"                                   # contains | equals
    name: str = "file_content"

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        if not self.expected:
            return None
        checked = 0
        failures: List[str] = []
        for path, want in self.expected.items():
            if path not in evidence.files:
                continue  # 该文件无证据，跳过（不臆断）
            checked += 1
            got = evidence.files[path]
            ok = (want in got) if self.mode == "contains" else (got == want)
            if not ok:
                failures.append(path)
        if checked == 0:
            return None  # 没有任何相关文件可判
        if failures:
            return Verdict(
                verdict=TargetVerdict.FAIL,
                reason=f"文件内容不符（{self.mode}）：{', '.join(failures)}",
            )
        return Verdict(
            verdict=TargetVerdict.PASS,
            reason=f"{checked} 个文件内容符合预期（{self.mode}）",
        )


@dataclass
class PatternVerifier:
    """期望某文本字段（默认 logs）匹配/不匹配正则。字段为空→None。"""

    pattern: str = ""
    field_name: str = "logs"
    should_match: bool = True
    name: str = "pattern"

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        if not self.pattern:
            return None
        text = str(getattr(evidence, self.field_name, "") or "")
        if not text:
            return None
        try:
            matched = re.search(self.pattern, text) is not None
        except re.error as exc:
            logger.warning("evolution.verifier: 非法正则 %r: %s", self.pattern, exc)
            return None
        ok = matched if self.should_match else (not matched)
        want = "匹配" if self.should_match else "不匹配"
        return Verdict(
            verdict=TargetVerdict.PASS if ok else TargetVerdict.FAIL,
            reason=f"日志{want} /{self.pattern}/：实际{'匹配' if matched else '未匹配'}",
        )


@dataclass
class MetricThresholdVerifier:
    """领域客观指标阈值（金融示例：夏普 > 1）。指标缺失→None。"""

    metric: str = ""
    min_value: float = 0.0
    name: str = "metric_threshold"

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        if not self.metric or self.metric not in evidence.metrics:
            return None
        value = evidence.metrics[self.metric]
        ok = value >= self.min_value
        return Verdict(
            verdict=TargetVerdict.PASS if ok else TargetVerdict.FAIL,
            reason=f"指标 {self.metric}={value}（阈值 ≥ {self.min_value}）",
        )


# ---------------------------------------------------------------------------
# B3/B4 · Verifier 编排
# ---------------------------------------------------------------------------

def _ensure_reason(verdict: Verdict, fallback: str) -> Verdict:
    """保证裁决理由非空（docs/13 §B3）。"""
    if not (verdict.reason and verdict.reason.strip()):
        verdict.reason = fallback
    return verdict


class Verifier:
    """独立校验器：客观验证器优先 → LLM 独立校验回退 → 探针回滚保护。

    - ``domain_verifiers``：客观验证器列表，任一给出非 None 裁决即采用（客观 > 模型）。
    - ``role`` / ``caller``：LLM 回退用 :class:`VerifierRole`（独立上下文，只看
      ``evidence.render()``）。无 caller 时 role 自动 fail-soft 产出 UNVERIFIED。
    - ``checkpoint_manager``：回滚保护用（默认惰性 :func:`spirit.checkpoint.get_manager`）。
    """

    def __init__(
        self,
        *,
        role: Optional[VerifierRole] = None,
        caller: Optional[LLMCaller] = None,
        domain_verifiers: Optional[List[DomainVerifier]] = None,
        checkpoint_manager: Any = None,
        enable_rollback: bool = True,
    ) -> None:
        self.role = role or VerifierRole(caller)
        self.domain_verifiers: List[DomainVerifier] = list(domain_verifiers or [])
        self._checkpoint = checkpoint_manager
        self.enable_rollback = enable_rollback

    # -- checkpoint 惰性获取 ------------------------------------------------
    def _manager(self) -> Any:
        if self._checkpoint is None:
            try:
                from spirit.checkpoint import get_manager
                self._checkpoint = get_manager()
            except Exception as exc:  # pragma: no cover - checkpoint 不可用时降级
                logger.warning("evolution.verifier: checkpoint 不可用，禁用回滚: %s", exc)
                self._checkpoint = False
        return self._checkpoint or None

    def add_verifier(self, verifier: DomainVerifier) -> "Verifier":
        self.domain_verifiers.append(verifier)
        return self

    # -- 主入口 ------------------------------------------------------------
    def verify(
        self,
        task: str,
        evidence: Any,
        *,
        probe: Optional[Probe] = None,
        use_llm: bool = True,
        task_id: str = "",
    ) -> Verdict:
        """对 ``task`` 依据 ``evidence`` 独立裁决，返回理由非空的 :class:`Verdict`。

        ``evidence`` 可为 :class:`Evidence` 或纯字符串（日志/证据文本，自动包装）。
        """
        ev = self._coerce_evidence(task, evidence)

        # B4：试探性探针 + 回滚保护
        if probe is not None:
            probe_verdict = self._run_probe_with_rollback(ev, probe, task_id)
            if probe_verdict is not None:
                return _ensure_reason(probe_verdict, "探针裁决（无理由）")

        # B5：客观验证器优先
        objective = self._run_domain_verifiers(task, ev, task_id)
        if objective is not None:
            return objective

        # B1/B3：LLM 独立校验回退
        if use_llm:
            return self.role.verify(task, ev.render(), task_id=task_id)

        return Verdict(
            verdict=TargetVerdict.UNVERIFIED,
            reason="无客观验证器可裁决且未启用 LLM 校验",
            task_id=task_id,
        )

    # -- 内部件 ------------------------------------------------------------
    @staticmethod
    def _coerce_evidence(task: str, evidence: Any) -> Evidence:
        if isinstance(evidence, Evidence):
            ev = evidence
        elif isinstance(evidence, dict):
            ev = Evidence.from_dict(evidence)
        else:
            ev = Evidence(logs=str(evidence or ""))
        if not ev.task:
            ev.task = task
        return ev

    def _run_domain_verifiers(
        self, task: str, ev: Evidence, task_id: str
    ) -> Optional[Verdict]:
        """按序跑客观验证器，返回首个决定性裁决；单个验证器异常则跳过。"""
        for verifier in self.domain_verifiers:
            try:
                verdict = verifier.verify(task, ev)
            except Exception as exc:
                logger.warning(
                    "evolution.verifier: 客观验证器 %s 异常，跳过: %s",
                    getattr(verifier, "name", verifier), exc,
                )
                continue
            if verdict is not None:
                verdict.task_id = task_id
                return _ensure_reason(verdict, "客观验证器裁决（无理由）")
        return None

    def _run_probe_with_rollback(
        self, ev: Evidence, probe: Probe, task_id: str
    ) -> Optional[Verdict]:
        """运行试探性探针；探针前后用 checkpoint 快照/回滚保护环境（B4）。

        - 探针抛异常 → 返回 UNVERIFIED（基础设施异常，绝不当 PASS/FAIL）。
        - 探针返回 Verdict → 采用之。
        - 探针返回 None → 返回 None，继续走客观/LLM 校验。
        """
        snapshot_id = self._snapshot(ev) if self.enable_rollback else None
        try:
            result = probe(ev)
            if isinstance(result, Verdict):
                result.task_id = task_id
                return result
            return None
        except Exception as exc:
            logger.warning("evolution.verifier: 探针执行异常 → UNVERIFIED: %s", exc)
            return Verdict(
                verdict=TargetVerdict.UNVERIFIED,
                reason=f"探针执行异常（基础设施错误）：{exc}",
                task_id=task_id,
            )
        finally:
            if snapshot_id:
                self._restore(snapshot_id)

    def _snapshot(self, ev: Evidence) -> Optional[str]:
        """对证据里涉及、且磁盘上真实存在的文件做快照，返回 checkpoint id。"""
        manager = self._manager()
        if manager is None:
            return None
        paths = [p for p in ev.files if p and Path(p).expanduser().is_file()]
        if not paths:
            return None
        try:
            res = manager.create(paths, description="verifier-probe", scope="evolution")
            if res.get("success"):
                return res["checkpoint"]["id"]
        except Exception as exc:  # pragma: no cover - checkpoint 异常降级
            logger.warning("evolution.verifier: 快照失败，跳过回滚保护: %s", exc)
        return None

    def _restore(self, snapshot_id: str) -> None:
        manager = self._manager()
        if manager is None:
            return
        try:
            manager.restore(snapshot_id)
        except Exception as exc:  # pragma: no cover - 回滚失败仅告警
            logger.warning("evolution.verifier: 回滚失败 %s: %s", snapshot_id, exc)


__all__ = [
    "Evidence",
    "Probe",
    "DomainVerifier",
    "ExitCodeVerifier",
    "FileContentVerifier",
    "PatternVerifier",
    "MetricThresholdVerifier",
    "Verifier",
]
