"""A switch cannot strand the keyboard on a peer with an unusable mouse."""
import unittest
from unittest.mock import patch

import test_engine
from kmlink.engine import POINTER_READY_CAPABILITY


class SwitchReliabilityTests(unittest.TestCase):
    build = test_engine.EngineTests.build
    hello = staticmethod(test_engine.EngineTests.hello)
    message = test_engine.EngineTests.message

    def verified_peer(self, role="controller"):
        result = self.build(role, handshake=False)
        engine, _, transport, _, _, _ = result
        self.message(engine, transport, self.hello(
            "receiver" if role == "controller" else "controller",
            capabilities=[POINTER_READY_CAPABILITY]))
        return result

    def test_controller_keeps_local_input_until_mouse_ready_ack(self):
        engine, native, transport, _, _, _ = self.verified_peer()
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            engine.enter_remote(999, 300)
            request = transport.sent[-1]
            self.assertEqual(request["type"], "enter")
            self.assertFalse(engine.remote_active)
            self.assertFalse(native.capturing)
            engine.enqueue("input", {"type": "key", "key": "char:a", "pressed": True})
            engine.poll()
            self.assertFalse(any(item["type"] == "key" for item in transport.sent))
            self.message(engine, transport, {"type": "entered", "entry_id": request["entry_id"]})
            self.assertTrue(engine.remote_active)
            self.assertTrue(native.capturing)

    def test_missing_ack_releases_pending_switch_without_capturing_keyboard(self):
        engine, native, transport, statuses, _, _ = self.verified_peer()
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            engine.enter_remote(999, 300)
        with patch("kmlink.engine.time.monotonic", return_value=101.6):
            engine.poll()
        self.assertIsNone(engine._entry_pending)
        self.assertFalse(engine.remote_active)
        self.assertFalse(native.capturing)
        self.assertTrue(engine.connected)
        self.assertEqual(transport.sent[-1]["type"], "leave")
        self.assertIn("未就绪", statuses[-1][1])

    def test_late_ack_cannot_capture_after_cancel_or_replace_new_request(self):
        engine, native, transport, _, _, _ = self.verified_peer()
        engine.enter_remote(999, 300)
        old = transport.sent[-1]["entry_id"]
        engine.return_local()
        engine.enter_remote(999, 300)
        new = transport.sent[-1]["entry_id"]
        self.message(engine, transport, {"type": "entered", "entry_id": old})
        self.assertFalse(native.capturing)
        self.assertEqual(engine._entry_pending["id"], new)
        self.message(engine, transport, {"type": "entered", "entry_id": new})
        self.assertTrue(native.capturing)
        engine.return_local()
        self.message(engine, transport, {"type": "entered", "entry_id": new})
        self.assertFalse(native.capturing)

    def test_receiver_retries_initial_move_and_acknowledges_actual_position(self):
        engine, native, transport, _, _, _ = self.verified_peer("receiver")
        posted = []
        native.move_to = lambda x, y: posted.append((x, y))
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            self.message(engine, transport, {"type": "enter", "side": "right",
                                            "rx": .5, "ry": .5, "entry_id": "a" * 32})
        self.assertFalse(any(item["type"] == "entered" for item in transport.sent))
        with patch("kmlink.engine.time.monotonic", return_value=100.2):
            engine.poll()
        self.assertEqual(len(posted), 2)
        native.xy = posted[-1]
        with patch("kmlink.engine.time.monotonic", return_value=100.3):
            engine.poll()
        self.assertEqual(transport.sent[-1], {"type": "entered", "entry_id": "a" * 32})
        self.assertTrue(engine.receiving)

    def test_receiver_initial_move_failure_returns_control_instead_of_acking(self):
        engine, native, transport, statuses, _, _ = self.verified_peer("receiver")
        native.move_to = lambda *_: None  # e.g. OS silently confines the pointer.
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            self.message(engine, transport, {"type": "enter", "side": "right",
                                            "rx": .5, "ry": .5, "entry_id": "a" * 32})
        with patch("kmlink.engine.time.monotonic", return_value=101.1):
            engine.poll()
        self.assertFalse(engine.receiving)
        self.assertTrue(engine.connected)
        self.assertFalse(any(item["type"] == "entered" for item in transport.sent))
        self.assertEqual(transport.sent[-1]["type"], "return")
        self.assertIn("未能进入", statuses[-1][1])

    def test_legacy_peer_still_switches_without_unknown_ack_messages(self):
        engine, native, transport, _, _, _ = self.build("receiver")
        self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
        self.assertTrue(engine.receiving)
        self.assertFalse(any(item["type"] == "entered" for item in transport.sent))
        engine, native, transport, _, _, _ = self.build()
        engine.enter_remote(999, 300)
        self.assertTrue(native.capturing)
        self.assertNotIn("entry_id", transport.sent[-1])

    def test_stalled_remote_pointer_returns_even_while_keyboard_connection_is_alive(self):
        engine, native, transport, statuses, _, _ = self.build("receiver")
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
        native.move_to = lambda *_: None
        with patch("kmlink.engine.time.monotonic", return_value=100.1):
            self.message(engine, transport, {"type": "move", "dx": 200, "dy": 0})
        with patch("kmlink.engine.time.monotonic", return_value=100.9):
            engine.poll()
        self.assertFalse(engine.receiving)
        self.assertTrue(engine.connected)
        self.assertEqual(transport.sent[-1]["type"], "return")
        self.assertIn("被限制", statuses[-1][1])

    def test_moving_pointer_does_not_trip_stall_guard(self):
        engine, native, transport, _, _, _ = self.build("receiver")
        with patch("kmlink.engine.time.monotonic", return_value=100.):
            self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
        for index in range(1, 8):
            with patch("kmlink.engine.time.monotonic", return_value=100. + index * .2):
                self.message(engine, transport, {"type": "move", "dx": 10, "dy": 2})
        self.assertTrue(engine.receiving)
        self.assertFalse(any(item["type"] == "return" for item in transport.sent))

    def test_removed_hook_is_restarted_on_next_explicit_switch(self):
        engine, native, transport, _, _, _ = self.build()
        native.needs_restart = True
        native.calls.clear()
        engine.enter_remote(999, 300)
        self.assertEqual(native.calls[:3], [("stop",), ("start",), ("capture", True)])
        self.assertTrue(engine.remote_active)


if __name__ == "__main__":
    unittest.main()
