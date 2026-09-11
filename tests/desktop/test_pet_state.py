"""PetState 推导测试。"""

import pytest
from spirit.desktop.pet_constants import PetState
from spirit.desktop.pet_state import derive_pet_state, todos_all_done


class TestDerivePetState:
    def test_default_idle(self):
        assert derive_pet_state() == PetState.IDLE

    def test_error_highest_priority(self):
        assert derive_pet_state(error=True, busy=True, reasoning=True) == PetState.FAILED

    def test_celebrate_over_busy(self):
        assert derive_pet_state(celebrate=True, busy=True) == PetState.JUMP

    def test_just_completed(self):
        assert derive_pet_state(just_completed=True) == PetState.WAVE

    def test_awaiting_input(self):
        assert derive_pet_state(awaiting_input=True) == PetState.WAITING

    def test_tool_running(self):
        assert derive_pet_state(tool_running=True) == PetState.RUN

    def test_reasoning(self):
        assert derive_pet_state(reasoning=True) == PetState.REVIEW

    def test_busy(self):
        assert derive_pet_state(busy=True) == PetState.RUN

    def test_priority_order(self):
        """验证完整优先级链。"""
        # error > celebrate
        assert derive_pet_state(error=True, celebrate=True) == PetState.FAILED
        # celebrate > just_completed
        assert derive_pet_state(celebrate=True, just_completed=True) == PetState.JUMP
        # just_completed > awaiting_input
        assert derive_pet_state(just_completed=True, awaiting_input=True) == PetState.WAVE
        # awaiting_input > tool_running
        assert derive_pet_state(awaiting_input=True, tool_running=True) == PetState.WAITING
        # tool_running > reasoning
        assert derive_pet_state(tool_running=True, reasoning=True) == PetState.RUN
        # reasoning > busy
        assert derive_pet_state(reasoning=True, busy=True) == PetState.REVIEW


class TestTodosAllDone:
    def test_empty_list(self):
        assert todos_all_done([]) is False
        assert todos_all_done(None) is False

    def test_all_completed(self):
        todos = [{"status": "completed"}, {"status": "completed"}]
        assert todos_all_done(todos) is True

    def test_some_pending(self):
        todos = [{"status": "completed"}, {"status": "pending"}]
        assert todos_all_done(todos) is False

    def test_with_cancelled(self):
        todos = [{"status": "completed"}, {"status": "cancelled"}]
        assert todos_all_done(todos) is True

    def test_with_objects(self):
        class Todo:
            def __init__(self, status):
                self.status = status
        todos = [Todo("completed"), Todo("cancelled")]
        assert todos_all_done(todos) is True
