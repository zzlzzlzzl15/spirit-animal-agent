"""spirit/storage/user_copy.py — plain-language copy for storage failure notices.

v0.4.0 (Hermes-aligned): user-friendly messages keyed by
classify_persistence_error's cause buckets.

Reference: hermes_state_user_copy.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class StorageFailure:
    """User-facing storage failure info."""

    def __init__(self, cause: str, summary: str, next_step: str, retryable: bool):
        self.cause = cause
        self.summary = summary
        self.next_step = next_step
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cause": self.cause,
            "summary": self.summary,
            "next_step": self.next_step,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"StorageFailure({self.cause!r}, retryable={self.retryable})"


# Plain-language copy per cause bucket. Keys must match
# classify_persistence_error() output values.
_COPY: Dict[str, Dict[str, Any]] = {
    "corrupt": {
        "summary": "\u672c\u5730\u4f1a\u8bdd\u6570\u636e\u5e93\u5df2\u635f\u574f\uff0c\u65e0\u6cd5\u7ee7\u7eed\u5199\u5165\u3002",
        "next_step": "\u5df2\u5c1d\u8bd5\u81ea\u52a8\u91cd\u5efa\u3002\u91cd\u542f Spirit \u540e\u53ef\u7ee7\u7eed\u4f7f\u7528\u3002",
        "retryable": False,
    },
    "replaced": {
        "summary": "\u672c\u5730\u4f1a\u8bdd\u6570\u636e\u5e93\u5df2\u88ab\u66ff\u6362\uff08\u53ef\u80fd\u7531\u53e6\u4e00\u8fdb\u7a0b\u91cd\u5efa\uff09\u3002",
        "next_step": "Spirit \u5c06\u81ea\u52a8\u91cd\u65b0\u52a0\u8f7d\u5e76\u91c7\u7528\u65b0\u7684\u6570\u636e\u5e93\u3002",
        "retryable": True,
    },
    "deleted_wal": {
        "summary": "WAL generation \u5df2\u88ab\u5916\u90e8\u8fdb\u7a0b\u5220\u9664\u3002",
        "next_step": "Spirit \u5c06\u81ea\u52a8\u91cd\u5f00\u6570\u636e\u5e93\u5e76\u91cd\u8bd5\u4e0a\u6b21\u5199\u5165\u3002",
        "retryable": True,
    },
    "lock": {
        "summary": "\u6570\u636e\u5e93\u6b63\u88ab\u5176\u4ed6\u8fdb\u7a0b\u5360\u7528\uff0c\u672c\u6b21\u5199\u5165\u672a\u5b8c\u6210\u3002",
        "next_step": "Spirit \u4f1a\u5728\u4e0b\u4e00\u8f6e\u81ea\u52a8\u91cd\u8bd5\u3002",
        "retryable": True,
    },
    "disk_full": {
        "summary": "\u78c1\u76d8\u7a7a\u95f4\u4e0d\u8db3\uff0c\u65e0\u6cd5\u5199\u5165\u6570\u636e\u5e93\u3002",
        "next_step": "\u8bf7\u6e05\u7406\u78c1\u76d8\u7a7a\u95f4\u540e\u7ee7\u7eed\u3002Spirit \u6682\u5b58\u4e8e JSONL \u5907\u4efd\u3002",
        "retryable": False,
    },
    "schema_drift": {
        "summary": "\u6570\u636e\u5e93 schema \u4e0e Spirit \u7248\u672c\u4e0d\u5339\u914d\u3002",
        "next_step": "Spirit \u6b63\u5728\u81ea\u52a8\u8fc1\u79fb schema\uff0c\u8bf7\u7a0d\u5019\u3002",
        "retryable": True,
    },
    "fts_corrupt": {
        "summary": "\u5168\u6587\u641c\u7d22\u7d22\u5f15\u635f\u574f\u3002",
        "next_step": "Spirit \u5c06\u91cd\u5efa FTS \u7d22\u5f15\u3002\u4f1a\u8bdd\u6570\u636e\u672a\u53d7\u5f71\u54cd\u3002",
        "retryable": True,
    },
    "compression_busy": {
        "summary": "\u53e6\u4e00\u538b\u7f29\u4efb\u52a1\u6b63\u5728\u8fdb\u884c\u4e2d\u3002",
        "next_step": "Spirit \u5c06\u7b49\u5f85\u4e0a\u4e00\u538b\u7f29\u5b8c\u6210\u540e\u518d\u5c1d\u8bd5\u3002",
        "retryable": True,
    },
    "compression_closed": {
        "summary": "\u538b\u7f29\u7a97\u53e3\u5df2\u5173\u95ed\uff0c\u672c\u8f6e\u538b\u7f29\u672a\u80fd\u5b8c\u6210\u3002",
        "next_step": "Spirit \u4f1a\u5728\u4e0b\u4e00\u8f6e\u91cd\u65b0\u89e6\u53d1\u538b\u7f29\u3002",
        "retryable": True,
    },
    "turn_lease_lost": {
        "summary": "\u672c\u8f6e turn \u5931\u53bb\u79df\u7ea6\uff08\u53ef\u80fd\u88ab\u5176\u4ed6\u8fdb\u7a0b\u62a2\u5360\uff09\u3002",
        "next_step": "Spirit \u5c06\u8ba9\u51fa\u672c\u8f6e\uff0c\u7531\u53e6\u4e00\u8fdb\u7a0b\u7ee7\u7eed\u3002",
        "retryable": False,
    },
    "unknown": {
        "summary": "\u6301\u4e45\u5316\u8fc7\u7a0b\u4e2d\u53d1\u751f\u672a\u77e5\u9519\u8bef\u3002",
        "next_step": "Spirit \u5df2\u8bb0\u5f55\u9519\u8bef\u5e76\u7ee7\u7eed\u3002\u53ef\u5728 state_meta.last_error \u67e5\u770b\u3002",
        "retryable": False,
    },
}


def describe_storage_failure(cause: str) -> str:
    """One-line user-facing summary for a classified failure cause."""
    entry = _COPY.get(cause, _COPY["unknown"])
    return entry["summary"]


def storage_failure_details(cause: str) -> StorageFailure:
    """Full structured info for UI / logs."""
    entry = _COPY.get(cause, _COPY["unknown"])
    return StorageFailure(
        cause=cause,
        summary=entry["summary"],
        next_step=entry["next_step"],
        retryable=entry["retryable"],
    )
