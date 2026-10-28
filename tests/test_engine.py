"""State-machine tests with no global hooks or desktop side effects."""

import platform
import unittest

from kmlink.engine import Bounds, Engine, MAX_CLIPBOARD_BYTES
from kmlink.native import validate_key_code


class FakeNative:
    def __init__(self):
        self.calls = []
        self.xy = (500, 300)
        self.capturing = False
        self.held = False
        self.keys = set()
        self.buttons = set()
        self.started = False

    def start(self):
        self.calls.append(("start",))
        self.started = True

    def stop(self):
        self.calls.append(("stop",))
        self.started = False

    def set_capturing(self, enabled):
        if enabled and self.held:
            raise RuntimeError("请先松开按键")
        self.calls.append(("capture", enabled))
        self.capturing = enabled

    def release_all(self):
        self.calls.append(("release_all",))
        self.keys.clear()
        self.buttons.clear()

    def position(self):
        return self.xy

    def move_to(self, x, y):
        self.calls.append(("move", x, y))
        self.xy = (x, y)

    def button(self, name, pressed):
        self.calls.append(("button", name, pressed))
        (self.buttons.add if pressed else self.buttons.discard)(name)

    def key(self, name, pressed):
        self.calls.append(("key", name, pressed))
        (self.keys.add if pressed else self.keys.discard)(name)

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


class FakeTransport:
    def __init__(self, on_message, on_status):
        self.on_message = on_message
        self.on_status = on_status
        self.sent = []
        self.stopped = False
        self.fail_send = False
        self.start_args = None

    def listen(self, *args):
        self.start_args = ("listen", *args)

    def connect(self, *args):
        self.start_args = ("connect", *args)

    def send(self, message):
        if self.fail_send:
            return False
        self.sent.append(message.copy())
        return True

    def stop(self):
        self.stopped = True


class EngineTests(unittest.TestCase):
    def build(self, role="controller", side="right", handshake=True):
        native = FakeNative()
        statuses, clipboards, transports = [], [], []

        def factory(on_message, on_status):
            result = FakeTransport(on_message, on_status)
            transports.append(result)
            return result

        engine = Engine(native, Bounds(0, 0, 1000, 600),
                        lambda state, detail: statuses.append((state, detail)),
                        clipboards.append, validate_key_code, factory)
        engine.start(role, "127.0.0.1", 24801, "unused by fake", side)
        transport = transports[-1]
        transport.on_status("connected", "connected")
        engine.poll()
        if handshake:
            transport.on_message(self.hello("receiver" if role == "controller" else "controller"))
            engine.poll()
        return engine, native, transport, statuses, clipboards, transports

    @staticmethod
    def hello(role, **overrides):
        value = {"type": "hello", "version": 1, "role": role,
                 "name": "Peer", "platform": "Darwin",
                 "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}}
        value.update(overrides)
        return value

    def message(self, engine, transport, message):
        transport.on_message(message)
        engine.poll()

    def test_hello_advertises_local_role_and_bounds(self):
        engine, native, transport, statuses, _, _ = self.build()
        self.assertEqual(transport.sent[0]["type"], "hello")
        self.assertEqual(transport.sent[0]["role"], "controller")
        self.assertEqual(transport.sent[0]["bounds"], engine.bounds.wire())
        self.assertTrue(native.started)
        self.assertEqual(statuses[-1][0], "ready")
        self.assertEqual(engine.peer_name, "Peer")

    def test_wrong_role_version_and_duplicate_hello_disconnect(self):
        invalid = [self.hello("controller"), self.hello("receiver", version=2),
                   self.hello("receiver", version="1"), self.hello("receiver", version=True)]
        for hello in invalid:
            with self.subTest(hello=hello):
                engine, native, transport, statuses, _, _ = self.build(handshake=False)
                self.message(engine, transport, hello)
                self.assertFalse(engine.connected)
                self.assertTrue(transport.stopped)
                self.assertEqual(statuses[-1][0], "error")
        engine, _, transport, statuses, _, _ = self.build()
        self.message(engine, transport, self.hello("receiver"))
        self.assertFalse(engine.connected)
        self.assertEqual(statuses[-1][0], "error")

    def test_input_before_hello_is_rejected(self):
        engine, native, transport, _, _, _ = self.build("receiver", handshake=False)
        self.message(engine, transport, {"type": "enter", "side": "right", "rx": 1, "ry": .5})
        self.assertFalse(engine.connected)
        self.assertFalse(engine.receiving)
        self.assertFalse(any(call[0] == "move" for call in native.calls))

    def test_all_four_controller_edges_enter_and_return(self):
        edges = {"right": (999, 300), "left": (0, 300),
                 "top": (500, 0), "bottom": (500, 599)}
        for side, xy in edges.items():
            with self.subTest(side=side):
                engine, native, transport, _, _, _ = self.build(side=side)
                engine._cooldown = 0
                engine.enqueue("input", {"type": "local_move", "x": xy[0], "y": xy[1]})
                engine.poll()
                self.assertTrue(engine.remote_active)
                self.assertTrue(native.capturing)
                self.assertEqual(transport.sent[-1]["type"], "enter")
                self.assertEqual(transport.sent[-1]["side"], side)
                self.message(engine, transport, {"type": "return", "rx": .4, "ry": .6})
                self.assertFalse(engine.remote_active)
                self.assertFalse(native.capturing)
                self.assertEqual(transport.sent[-1], {"type": "leave"})
                x, y = native.position()
                if side == "right":
                    self.assertEqual(x, 971)
                elif side == "left":
                    self.assertEqual(x, 28)
                elif side == "top":
                    self.assertEqual(y, 28)
                else:
                    self.assertEqual(y, 571)

    def test_corner_other_screen_and_cooldown_do_not_enter(self):
        for xy in ((999, 0), (999, 599), (1200, 300), (-1, 300)):
            with self.subTest(xy=xy):
                engine, _, transport, _, _, _ = self.build()
                engine._cooldown = 0
                engine.enqueue("input", {"type": "local_move", "x": xy[0], "y": xy[1]})
                engine.poll()
                self.assertFalse(engine.remote_active)
                self.assertFalse(any(msg["type"] == "enter" for msg in transport.sent))
        engine, _, _, _, _, _ = self.build()
        engine.enqueue("input", {"type": "local_move", "x": 999, "y": 300})
        engine.poll()
        self.assertFalse(engine.remote_active)

    def test_all_four_receiver_edges_return_and_release(self):
        moves = {"right": (-50, 0), "left": (50, 0),
                 "top": (0, 50), "bottom": (0, -50)}
        for side, delta in moves.items():
            with self.subTest(side=side):
                engine, native, transport, _, _, _ = self.build("receiver")
                self.message(engine, transport, {"type": "enter", "side": side, "rx": .5, "ry": .5})
                self.message(engine, transport, {"type": "key", "key": "named:ctrl_l", "pressed": True})
                self.message(engine, transport, {"type": "button", "button": "left", "pressed": True})
                self.assertTrue(engine.receiving)
                self.assertTrue(native.keys)
                self.message(engine, transport, {"type": "move", "dx": delta[0], "dy": delta[1]})
                self.assertFalse(engine.receiving)
                self.assertFalse(native.keys)
                self.assertFalse(native.buttons)
                self.assertEqual(transport.sent[-1]["type"], "return")
                self.assertTrue(0 <= transport.sent[-1]["rx"] <= 1)
                self.assertTrue(0 <= transport.sent[-1]["ry"] <= 1)

    def test_disconnect_releases_receiver_and_controller(self):
        for role in ("receiver", "controller"):
            with self.subTest(role=role):
                engine, native, transport, statuses, _, _ = self.build(role)
                if role == "receiver":
                    self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
                    self.message(engine, transport, {"type": "key", "key": "char:a", "pressed": True})
                else:
                    engine.enter_remote(999, 300)
                transport.on_status("disconnected", "network lost")
                engine.poll()
                self.assertFalse(engine.connected)
                self.assertFalse(engine.remote_active)
                self.assertFalse(engine.receiving)
                self.assertFalse(native.capturing)
                self.assertFalse(native.keys)
                self.assertFalse(native.started)
                self.assertTrue(transport.stopped)
                self.assertEqual(statuses[-1], ("disconnected", "network lost"))

    def test_leave_releases_buttons_and_ignores_inflight_input(self):
        engine, native, transport, _, _, _ = self.build("receiver")
        self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
        self.message(engine, transport, {"type": "key", "key": "named:shift_l", "pressed": True})
        self.message(engine, transport, {"type": "button", "button": "left", "pressed": True})
        self.message(engine, transport, {"type": "leave"})
        self.assertFalse(engine.receiving)
        self.assertFalse(native.keys)
        self.assertFalse(native.buttons)
        native.calls.clear()
        self.message(engine, transport, {"type": "move", "dx": 40, "dy": 20})
        self.message(engine, transport, {"type": "key", "key": "char:a", "pressed": True})
        self.assertFalse(native.calls)
        self.assertTrue(engine.connected)

    def test_unconnected_outer_edge_clamps_without_return(self):
        engine, native, transport, _, _, _ = self.build("receiver")
        self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
        self.message(engine, transport, {"type": "move", "dx": 2000, "dy": 0})
        self.assertEqual(native.position()[0], engine.bounds.width - 1)
        self.assertTrue(engine.receiving)
        self.assertFalse(any(msg["type"] == "return" for msg in transport.sent))

    def test_nonfinite_and_bad_input_disconnect_before_injection(self):
        bad = [{"type": "move", "dx": value, "dy": 0}
               for value in (float("nan"), float("inf"), -float("inf"), True, "1", 4097)]
        bad += [{"type": "scroll", "dx": 0, "dy": 121},
                {"type": "button", "button": "unknown", "pressed": True},
                {"type": "button", "button": "left", "pressed": 1},
                {"type": "key", "key": "char:ab", "pressed": True},
                {"type": "key", "key": "named:ctrl_l", "pressed": "true"},
                {"type": "unknown"}]
        for message in bad:
            with self.subTest(message=message):
                engine, native, transport, statuses, _, _ = self.build("receiver")
                self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
                native.calls.clear()
                self.message(engine, transport, message)
                self.assertFalse(engine.connected)
                self.assertFalse(engine.receiving)
                self.assertEqual(statuses[-1][0], "error")
                self.assertFalse(any(call[0] in {"key", "move", "scroll", "button"} for call in native.calls))

    def test_invalid_bounds_and_entry_coordinates_disconnect(self):
        for bounds in ({"x": 0, "y": 0, "width": True, "height": 600},
                       {"x": 0, "y": 0, "width": 0, "height": 600}):
            engine, _, transport, _, _, _ = self.build(handshake=False)
            self.message(engine, transport, self.hello("receiver", bounds=bounds))
            self.assertFalse(engine.connected)
        for coordinate in (-.1, 1.1, float("nan"), True):
            engine, native, transport, _, _, _ = self.build("receiver")
            self.message(engine, transport, {"type": "enter", "side": "right", "rx": coordinate, "ry": .5})
            self.assertFalse(engine.connected)
            self.assertFalse(engine.receiving)

    def test_stale_generation_cannot_change_new_session(self):
        engine, native, old_transport, _, _, transports = self.build()
        engine.start("receiver", "127.0.0.1", 24801, "unused", "left")
        current_transport = transports[-1]
        old_transport.on_status("error", "stale failure")
        old_transport.on_message({"type": "enter", "side": "right", "rx": .5, "ry": .5})
        current_transport.on_status("connected", "new connection")
        current_transport.on_message(self.hello("controller"))
        engine.poll()
        self.assertTrue(engine.connected)
        self.assertFalse(current_transport.stopped)
        self.assertFalse(engine.receiving)
        self.assertEqual(engine.role, "receiver")

    def test_clipboard_requires_opt_in_and_suppresses_echo(self):
        engine, _, transport, _, clipboards, _ = self.build()
        engine.clipboard_changed("private")
        self.message(engine, transport, {"type": "clipboard", "text": "remote private"})
        self.assertEqual(clipboards, [])
        self.assertFalse(any(msg["type"] == "clipboard" for msg in transport.sent))
        engine.share_clipboard = True
        engine.clipboard_changed("local text")
        self.assertEqual(transport.sent[-1], {"type": "clipboard", "text": "local text"})
        count = len(transport.sent)
        engine.clipboard_changed("local text")
        self.assertEqual(len(transport.sent), count)
        self.message(engine, transport, {"type": "clipboard", "text": "远端文本"})
        self.assertEqual(clipboards, ["远端文本"])
        engine.clipboard_changed("远端文本")
        self.assertEqual(len(transport.sent), count)

    def test_clipboard_size_counts_utf8_bytes(self):
        engine, _, transport, _, clipboards, _ = self.build()
        engine.share_clipboard = True
        before = len(transport.sent)
        oversized = "字" * (MAX_CLIPBOARD_BYTES // 3 + 1)
        engine.clipboard_changed(oversized)
        self.assertEqual(len(transport.sent), before)
        self.message(engine, transport, {"type": "clipboard", "text": oversized})
        self.assertFalse(engine.connected)
        self.assertEqual(clipboards, [])

    def test_held_keys_refuse_capture_without_sending_enter(self):
        engine, native, transport, statuses, _, _ = self.build()
        native.held = True
        engine.enter_remote(999, 300)
        self.assertFalse(engine.remote_active)
        self.assertFalse(native.capturing)
        self.assertTrue(engine.connected)
        self.assertEqual(statuses[-1][0], "notice")
        self.assertFalse(any(msg["type"] == "enter" for msg in transport.sent))

    def test_mapping_release_retains_key_from_press(self):
        engine, _, transport, _, _, _ = self.build()
        engine._peer_platform = "Darwin" if platform.system() != "Darwin" else "Windows"
        engine.map_ctrl_cmd = True
        engine.enter_remote(999, 300)
        engine.enqueue("input", {"type": "key", "key": "named:ctrl_l", "pressed": True})
        engine.poll()
        self.assertEqual(transport.sent[-1]["key"], "named:cmd_l")
        engine.map_ctrl_cmd = False
        engine.enqueue("input", {"type": "key", "key": "named:ctrl_l", "pressed": False})
        engine.poll()
        self.assertEqual(transport.sent[-1]["key"], "named:cmd_l")

    def test_transport_failure_and_queue_overflow_release_control(self):
        engine, native, transport, statuses, _, _ = self.build()
        engine.enter_remote(999, 300)
        transport.fail_send = True
        engine.enqueue("input", {"type": "key", "key": "char:a", "pressed": True})
        engine.poll()
        self.assertFalse(engine.remote_active)
        self.assertFalse(native.capturing)
        self.assertEqual(statuses[-1][0], "error")
        engine, native, transport, statuses, _, _ = self.build()
        engine.enter_remote(999, 300)
        engine._overflow.set()
        engine.poll()
        self.assertFalse(engine.connected)
        self.assertFalse(native.capturing)
        self.assertEqual(statuses[-1][0], "error")

    def test_emergency_reclaims_controller_or_stops_receiver(self):
        for role in ("controller", "receiver"):
            engine, native, transport, _, _, _ = self.build(role)
            if role == "controller":
                engine.enter_remote(999, 300)
            else:
                self.message(engine, transport, {"type": "enter", "side": "right", "rx": .5, "ry": .5})
            engine.enqueue("emergency")
            engine.poll()
            self.assertFalse(engine.remote_active)
            self.assertFalse(engine.receiving)
            self.assertFalse(native.capturing)
            self.assertEqual(engine.connected, role == "controller")


if __name__ == "__main__":
    unittest.main()
