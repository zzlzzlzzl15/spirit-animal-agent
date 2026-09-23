"""tests/skills_hub/test_provenance.py — 写入来源 ContextVar + 安装来源记录。

对标 Hermes ``tests/tools/test_skill_provenance.py``（``ContextVar`` 区分后台评审 fork 与前台
写入），并覆盖 Spirit 追加的安装来源记录器（``record_install`` / ``get_install`` /
``clear_install`` / ``all_installs`` → ``<hub>/provenance.json``）。
"""

from __future__ import annotations

import contextvars
import json

from spirit.skills_hub import paths, provenance


# ---------------------------------------------------------------------------
# 写入来源 ContextVar（对标 Hermes）
# ---------------------------------------------------------------------------

class TestWriteOrigin:
    def test_set_and_get(self):
        token = provenance.set_current_write_origin(provenance.BACKGROUND_REVIEW)
        try:
            assert provenance.get_current_write_origin() == "background_review"
        finally:
            provenance.reset_current_write_origin(token)

    def test_reset_restores_prior(self):
        outer = provenance.set_current_write_origin("assistant_tool")
        try:
            inner = provenance.set_current_write_origin(provenance.BACKGROUND_REVIEW)
            try:
                assert provenance.get_current_write_origin() == "background_review"
            finally:
                provenance.reset_current_write_origin(inner)
            assert provenance.get_current_write_origin() == "assistant_tool"
        finally:
            provenance.reset_current_write_origin(outer)

    def test_default_is_foreground(self):
        assert provenance.get_current_write_origin() == provenance.FOREGROUND

    def test_is_background_review_truthy_only_for_review(self):
        for origin, expected in (
            ("foreground", False),
            ("assistant_tool", False),
            ("random_other_value", False),
            (provenance.BACKGROUND_REVIEW, True),
        ):
            token = provenance.set_current_write_origin(origin)
            try:
                assert provenance.is_background_review() is expected
            finally:
                provenance.reset_current_write_origin(token)

    def test_empty_origin_falls_back_to_foreground(self):
        token = provenance.set_current_write_origin("")
        try:
            assert provenance.get_current_write_origin() == "foreground"
        finally:
            provenance.reset_current_write_origin(token)

    def test_context_isolation_between_copies(self):
        original = provenance.get_current_write_origin()

        def _run_in_copy():
            provenance.set_current_write_origin(provenance.BACKGROUND_REVIEW)
            return provenance.get_current_write_origin()

        ctx = contextvars.copy_context()
        inside = ctx.run(_run_in_copy)
        assert inside == provenance.BACKGROUND_REVIEW
        # 父上下文不受影响。
        assert provenance.get_current_write_origin() == original


# ---------------------------------------------------------------------------
# 安装来源记录（<hub>/provenance.json）
# ---------------------------------------------------------------------------

class TestInstallRecord:
    def test_record_and_get(self, skills_home):
        provenance.record_install(
            "my-skill", source="taps/main", identifier="owner/repo",
            trust_level="community", source_url="https://x", content_hash="sha256:abc",
        )
        rec = provenance.get_install("my-skill")
        assert rec is not None
        assert rec["source"] == "taps/main"
        assert rec["identifier"] == "owner/repo"
        assert rec["trust_level"] == "community"
        assert rec["content_hash"] == "sha256:abc"
        assert "installed_at" in rec

    def test_get_missing_returns_none(self, skills_home):
        assert provenance.get_install("ghost") is None

    def test_persists_under_hub(self, skills_home):
        provenance.record_install("s", source="src")
        assert paths.provenance_file().exists()
        data = json.loads(paths.provenance_file().read_text(encoding="utf-8"))
        assert "s" in data

    def test_record_is_idempotent_overwrite(self, skills_home):
        provenance.record_install("s", source="old")
        provenance.record_install("s", source="new")
        assert provenance.get_install("s")["source"] == "new"
        assert len(provenance.all_installs()) == 1

    def test_clear_install(self, skills_home):
        provenance.record_install("s", source="src")
        provenance.clear_install("s")
        assert provenance.get_install("s") is None

    def test_clear_missing_is_noop(self, skills_home):
        provenance.clear_install("ghost")  # 不抛
        assert provenance.all_installs() == {}

    def test_empty_name_noop(self, skills_home):
        provenance.record_install("", source="src")
        assert provenance.all_installs() == {}

    def test_all_installs(self, skills_home):
        provenance.record_install("a", source="s1")
        provenance.record_install("b", source="s2")
        assert set(provenance.all_installs()) == {"a", "b"}

    def test_corrupt_file_degrades(self, skills_home):
        paths.provenance_file().parent.mkdir(parents=True, exist_ok=True)
        paths.provenance_file().write_text("{ bad json", encoding="utf-8")
        assert provenance.all_installs() == {}
        assert provenance.get_install("x") is None
