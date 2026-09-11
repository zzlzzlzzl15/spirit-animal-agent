"""PetEngine 测试。"""

import pytest
from spirit.desktop.pet_constants import PetState
from spirit.desktop.pet_engine import PetEngine, ActivitySignals, PetPreferences


# ---------------------------------------------------------------------------
# ActivitySignals
# ---------------------------------------------------------------------------

class TestActivitySignals:
    def test_default_all_false(self):
        s = ActivitySignals()
        assert s.busy is False
        assert s.error is False
        assert s.celebrate is False

    def test_reset_transient(self):
        s = ActivitySignals()
        s.error = True
        s.celebrate = True
        s.just_completed = True
        s.busy = True  # 持久信号
        s.reset_transient()
        assert s.error is False
        assert s.celebrate is False
        assert s.just_completed is False
        assert s.busy is True  # 不被清除

    def test_to_dict(self):
        s = ActivitySignals(busy=True, reasoning=True)
        d = s.to_dict()
        assert d["busy"] is True
        assert d["reasoning"] is True
        assert d["error"] is False


# ---------------------------------------------------------------------------
# PetPreferences
# ---------------------------------------------------------------------------

class TestPetPreferences:
    def test_defaults(self):
        p = PetPreferences()
        assert p.scale == 0.75
        assert p.active_slug == ""

    def test_to_dict_from_dict(self):
        p = PetPreferences(active_slug="fox", scale=0.8, pos_x=100, pos_y=200)
        d = p.to_dict()
        p2 = PetPreferences.from_dict(d)
        assert p2.active_slug == "fox"
        assert p2.scale == 0.8
        assert p2.pos_x == 100


# ---------------------------------------------------------------------------
# PetEngine
# ---------------------------------------------------------------------------

class TestPetEngine:
    def test_init_default_idle(self):
        engine = PetEngine()
        assert engine.current_state == PetState.IDLE

    def test_update_signals_busy(self):
        engine = PetEngine()
        new_state = engine.update_signals(busy=True)
        assert new_state == PetState.RUN

    def test_update_signals_error(self):
        engine = PetEngine()
        new_state = engine.update_signals(error=True)
        assert new_state == PetState.FAILED

    def test_update_signals_reasoning(self):
        engine = PetEngine()
        new_state = engine.update_signals(reasoning=True)
        assert new_state == PetState.REVIEW

    def test_update_signals_awaiting_input(self):
        engine = PetEngine()
        new_state = engine.update_signals(awaiting_input=True)
        assert new_state == PetState.WAITING

    def test_update_signals_celebrate(self):
        engine = PetEngine()
        new_state = engine.update_signals(celebrate=True)
        assert new_state == PetState.JUMP

    def test_on_turn_complete(self):
        engine = PetEngine()
        engine.update_signals(busy=True)
        assert engine.current_state == PetState.RUN
        engine.on_turn_complete(response_text="done")
        assert engine.current_state == PetState.WAVE

    def test_on_turn_complete_error(self):
        engine = PetEngine()
        engine.update_signals(busy=True)
        engine.on_turn_complete(error=True)
        assert engine.current_state == PetState.FAILED

    def test_on_tool_start_complete(self):
        engine = PetEngine()
        engine.on_tool_start("search")
        assert engine.current_state == PetState.RUN
        engine.on_tool_complete("search")
        assert engine.current_state == PetState.IDLE

    def test_on_reasoning(self):
        engine = PetEngine()
        engine.on_reasoning_start()
        assert engine.current_state == PetState.REVIEW
        engine.on_reasoning_complete()
        assert engine.current_state == PetState.IDLE

    def test_idle_reset(self):
        engine = PetEngine()
        engine.update_signals(busy=True, reasoning=True)
        assert engine.current_state != PetState.IDLE
        engine.idle()
        assert engine.current_state == PetState.IDLE

    def test_set_state_direct(self):
        engine = PetEngine()
        engine.set_state(PetState.JUMP)
        assert engine.current_state == PetState.JUMP

    def test_state_callbacks(self):
        engine = PetEngine()
        transitions = []
        engine.on_state_change(lambda old, new, ctx: transitions.append((old.value, new.value)))
        engine.update_signals(busy=True)
        assert len(transitions) == 1
        assert transitions[0] == ("idle", "run")

    def test_get_full_status(self):
        engine = PetEngine()
        status = engine.get_full_status()
        assert "pet" in status
        assert "state" in status
        assert "signals" in status
        assert "preferences" in status
        assert status["state"]["current"] == "idle"

    def test_list_pets_empty(self):
        engine = PetEngine()
        pets = engine.list_pets()
        assert isinstance(pets, list)

    def test_set_scale(self):
        engine = PetEngine()
        engine.set_scale(1.5)
        assert engine.preferences.scale == 1.5

    def test_set_scale_clamped(self):
        engine = PetEngine()
        engine.set_scale(10.0)
        assert engine.preferences.scale == 3.0
        engine.set_scale(0.01)
        assert engine.preferences.scale == 0.2

    def test_set_position(self):
        engine = PetEngine()
        engine.set_position(100, 200)
        assert engine.preferences.pos_x == 100
        assert engine.preferences.pos_y == 200

    def test_stats_tracking(self):
        engine = PetEngine()
        engine.on_turn_complete(response_text="hello")
        engine.on_tool_complete("tool1")
        engine.on_turn_complete(error=True)
        assert engine._stats["total_turns"] == 2
        assert engine._stats["total_tool_calls"] == 1
        assert engine._stats["total_errors"] == 1
