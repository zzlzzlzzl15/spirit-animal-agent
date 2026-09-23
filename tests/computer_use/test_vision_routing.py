"""``spirit.computer_use.vision_routing`` 决策链测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use_vision_routing.py``（``TestExplicitAuxVisionOverride``
/ ``TestRouteDecision`` / ``TestLookupHelpers`` / ``TestModuleSurface``）。

关键差异：Hermes 用**模块级 patchable 函数** + models.dev / image_routing 元数据源；Spirit 把三个
查询做成**可注入 keyword seam**（``user_declared_lookup`` / ``supports_vision_lookup`` /
``accepts_tool_image_lookup``），默认实现只读 ``cfg``（无外部依赖）。故本测试注入假 seam 离线断言
整条决策链，而非 monkeypatch 模块函数——这正是「可测试抽象层」设计约束的体现。

决策链（fail-closed 到辅助路由）：
  ① 显式 auxiliary.vision → True
  ② user_declared True → False；False → True
  ③ accepts_tool_image None|False → True
  ④ supports_vision True → False；否则（False/None）→ True
"""

from __future__ import annotations

import pytest

from spirit.computer_use import vision_routing as vr
from spirit.computer_use.vision_routing import should_route_capture_to_aux_vision


def _const(value):
    """造一个恒返回 ``value`` 的 seam（忽略入参）。"""
    def _fn(*args, **kwargs):
        return value
    return _fn


def _raiser(exc=RuntimeError("lookup down")):
    """造一个收到调用即抛 ``exc`` 的 seam（验证异常折叠成 None）。"""
    def _fn(*args, **kwargs):
        raise exc
    return _fn


# ---------------------------------------------------------------------------
# _explicit_aux_vision_override
# ---------------------------------------------------------------------------

class TestExplicitAuxVisionOverride:
    def test_none_cfg_false(self):
        assert vr._explicit_aux_vision_override(None) is False

    def test_non_dict_cfg_false(self):
        assert vr._explicit_aux_vision_override("nope") is False

    def test_missing_aux_block_false(self):
        assert vr._explicit_aux_vision_override({}) is False

    def test_auto_provider_no_model_false(self):
        assert vr._explicit_aux_vision_override({"auxiliary": {"vision": {"provider": "auto"}}}) is False

    def test_empty_provider_false(self):
        assert vr._explicit_aux_vision_override({"auxiliary": {"vision": {"provider": ""}}}) is False

    def test_explicit_provider_true(self):
        cfg = {"auxiliary": {"vision": {"provider": "openai"}}}
        assert vr._explicit_aux_vision_override(cfg) is True

    def test_model_only_true(self):
        assert vr._explicit_aux_vision_override({"auxiliary": {"vision": {"model": "gpt-4o"}}}) is True

    def test_base_url_only_true(self):
        cfg = {"auxiliary": {"vision": {"base_url": "http://x"}}}
        assert vr._explicit_aux_vision_override(cfg) is True

    def test_auto_provider_with_model_true(self):
        cfg = {"auxiliary": {"vision": {"provider": "auto", "model": "gpt-4o"}}}
        assert vr._explicit_aux_vision_override(cfg) is True

    def test_non_dict_aux_false(self):
        assert vr._explicit_aux_vision_override({"auxiliary": "x"}) is False

    def test_non_dict_vision_false(self):
        assert vr._explicit_aux_vision_override({"auxiliary": {"vision": "x"}}) is False


# ---------------------------------------------------------------------------
# 默认 lookup helpers
# ---------------------------------------------------------------------------

class TestLookupHelpers:
    def test_default_supports_vision_is_none(self):
        assert vr._default_supports_vision("openai", "gpt-4o", {}) is None

    def test_default_accepts_tool_image_is_none(self):
        assert vr._default_accepts_tool_image("openai", "gpt-4o") is None

    def test_user_declared_provider_model_key(self):
        cfg = {"models": {"openai/gpt-4o": {"supports_vision": True}}}
        assert vr._default_user_declared(cfg, "openai", "gpt-4o") is True

    def test_user_declared_model_only_key(self):
        cfg = {"models": {"gpt-4o": {"supports_vision": False}}}
        assert vr._default_user_declared(cfg, "openai", "gpt-4o") is False

    def test_user_declared_provider_key_wins(self):
        cfg = {"models": {
            "openai/gpt-4o": {"supports_vision": True},
            "gpt-4o": {"supports_vision": False},
        }}
        assert vr._default_user_declared(cfg, "openai", "gpt-4o") is True

    def test_user_declared_top_level_model(self):
        cfg = {"model": {"supports_vision": True}}
        assert vr._default_user_declared(cfg, "openai", "gpt-4o") is True

    def test_user_declared_none_when_absent(self):
        assert vr._default_user_declared({}, "openai", "gpt-4o") is None

    def test_user_declared_none_when_non_bool(self):
        cfg = {"models": {"gpt-4o": {"supports_vision": "yes"}}}
        assert vr._default_user_declared(cfg, "openai", "gpt-4o") is None

    def test_user_declared_none_cfg(self):
        assert vr._default_user_declared(None, "openai", "gpt-4o") is None


# ---------------------------------------------------------------------------
# should_route_capture_to_aux_vision — 决策链
# ---------------------------------------------------------------------------

class TestRouteDecision:
    def test_explicit_aux_overrides_everything(self):
        cfg = {"auxiliary": {"vision": {"provider": "openai"}}}
        out = should_route_capture_to_aux_vision(
            "openai", "gpt-4o", cfg,
            user_declared_lookup=_const(True),
            supports_vision_lookup=_const(True),
            accepts_tool_image_lookup=_const(True),
        )
        assert out is True

    def test_user_declared_true_multimodal(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None, user_declared_lookup=_const(True),
        ) is False

    def test_user_declared_false_aux(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None, user_declared_lookup=_const(False),
        ) is True

    def test_accepts_none_aux(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(None),
        ) is True

    def test_accepts_false_aux(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(False),
        ) is True

    def test_accepts_true_supports_true_multimodal(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(True),
            supports_vision_lookup=_const(True),
        ) is False

    def test_accepts_true_supports_false_aux(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(True),
            supports_vision_lookup=_const(False),
        ) is True

    def test_accepts_true_supports_none_fail_closed_aux(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(True),
            supports_vision_lookup=_const(None),
        ) is True

    def test_default_seams_fail_closed_to_aux(self):
        # 不注入任何 seam → user_declared 读 cfg(None)、accepts/supports 恒 None → 走辅助
        assert should_route_capture_to_aux_vision("p", "m", None) is True

    def test_default_user_declared_from_cfg_short_circuits(self):
        # cfg 声明 supports_vision True → 默认 user_declared_lookup 读到 True → 多模态（早于 accepts）
        cfg = {"models": {"p/m": {"supports_vision": True}}}
        assert should_route_capture_to_aux_vision("p", "m", cfg) is False


# ---------------------------------------------------------------------------
# 异常折叠（seam 抛错 → 当作 None）
# ---------------------------------------------------------------------------

class TestLookupExceptionFolding:
    def test_user_lookup_exception_folds_to_none(self):
        # user_declared 抛错 → None；accepts True + supports True → 多模态 False
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_raiser(),
            accepts_tool_image_lookup=_const(True),
            supports_vision_lookup=_const(True),
        ) is False

    def test_accepts_lookup_exception_folds_to_none(self):
        # accepts 抛错 → None → 走辅助 True（即便 supports True）
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_raiser(),
            supports_vision_lookup=_const(True),
        ) is True

    def test_supports_lookup_exception_folds_to_none(self):
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(None),
            accepts_tool_image_lookup=_const(True),
            supports_vision_lookup=_raiser(),
        ) is True


# ---------------------------------------------------------------------------
# seam 调用契约（入参形状）
# ---------------------------------------------------------------------------

class TestSeamCallContract:
    def test_seams_receive_expected_args(self):
        seen: dict = {}

        def user_lookup(cfg, provider, model):
            seen["user"] = (cfg, provider, model)
            return None

        def accepts_lookup(provider, model):
            seen["accepts"] = (provider, model)
            return True

        def supports_lookup(provider, model, cfg):
            seen["supports"] = (provider, model, cfg)
            return True

        cfg = {"k": "v"}
        should_route_capture_to_aux_vision(
            "prov", "mod", cfg,
            user_declared_lookup=user_lookup,
            accepts_tool_image_lookup=accepts_lookup,
            supports_vision_lookup=supports_lookup,
        )
        assert seen["user"] == (cfg, "prov", "mod")
        assert seen["accepts"] == ("prov", "mod")
        assert seen["supports"] == ("prov", "mod", cfg)

    def test_short_circuit_skips_downstream_lookups(self):
        # user_declared True → 立即 return False，不再调用 accepts/supports
        called = []
        assert should_route_capture_to_aux_vision(
            "p", "m", None,
            user_declared_lookup=_const(True),
            accepts_tool_image_lookup=lambda *a: called.append("accepts") or True,
            supports_vision_lookup=lambda *a: called.append("supports") or True,
        ) is False
        assert called == []


# ---------------------------------------------------------------------------
# 模块表面
# ---------------------------------------------------------------------------

class TestModuleSurface:
    def test_all_exports_only_decision_fn(self):
        assert vr.__all__ == ["should_route_capture_to_aux_vision"]

    def test_helpers_accessible_as_attributes(self):
        for name in (
            "_explicit_aux_vision_override", "_default_user_declared",
            "_default_supports_vision", "_default_accepts_tool_image",
        ):
            assert callable(getattr(vr, name))

    def test_decision_fn_is_callable(self):
        assert callable(should_route_capture_to_aux_vision)
