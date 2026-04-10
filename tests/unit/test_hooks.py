"""Tests for the plugin hook system."""

from yokai.core.hooks import HookRegistry


class TestHookRegistry:
    def test_register_and_emit_calls_callback(self):
        reg = HookRegistry()
        received = []
        reg.register("test_event", lambda payload: received.append(payload))
        reg.emit("test_event", {"key": "value"})
        assert received == [{"key": "value"}]

    def test_multiple_callbacks_all_called(self):
        reg = HookRegistry()
        received = []
        reg.register("event", lambda p: received.append("first"))
        reg.register("event", lambda p: received.append("second"))
        reg.emit("event", {})
        assert received == ["first", "second"]

    def test_emit_with_no_listeners_is_noop(self):
        reg = HookRegistry()
        reg.emit("nobody-listens", {"x": 1})

    def test_failing_hook_does_not_break_others(self):
        reg = HookRegistry()
        called = []

        def bad_hook(p):
            raise RuntimeError("intentional")

        reg.register("event", bad_hook)
        reg.register("event", lambda p: called.append("good"))
        reg.emit("event", {})
        assert called == ["good"]

    def test_count_reflects_registered_callbacks(self):
        reg = HookRegistry()
        assert reg.count("event") == 0
        reg.register("event", lambda p: None)
        reg.register("event", lambda p: None)
        assert reg.count("event") == 2

    def test_clear_removes_all(self):
        reg = HookRegistry()
        reg.register("a", lambda p: None)
        reg.register("b", lambda p: None)
        reg.clear()
        assert reg.count("a") == 0
        assert reg.count("b") == 0

    def test_events_are_independent(self):
        reg = HookRegistry()
        called_a = []
        called_b = []
        reg.register("a", lambda p: called_a.append(1))
        reg.register("b", lambda p: called_b.append(1))
        reg.emit("a", {})
        assert called_a == [1]
        assert called_b == []
