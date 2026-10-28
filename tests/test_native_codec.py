import types
import threading
import unittest

from kmlink.native import NativeInput, encode_key, validate_key_code


class KeyCodecTests(unittest.TestCase):
    def test_control_characters_recover_shortcut_letters(self):
        self.assertEqual(encode_key(types.SimpleNamespace(char="\x03")), "char:c")
        self.assertEqual(encode_key(types.SimpleNamespace(char="\x1a")), "char:z")

    def test_named_keys_and_unicode(self):
        self.assertEqual(encode_key(types.SimpleNamespace(name="ctrl_l")), "named:ctrl_l")
        self.assertEqual(encode_key(types.SimpleNamespace(char="中")), "char:中")
        self.assertTrue(validate_key_code("named:f24"))
        self.assertTrue(validate_key_code("char:😀"))

    def test_rejects_virtual_keys_and_multi_character_payloads(self):
        for code in (None, 42, "vk:91", "named:danger", "char:", "char:hello",
                     "char:\n", "char:\ud800"):
            self.assertFalse(validate_key_code(code), repr(code))
        self.assertIsNone(encode_key(types.SimpleNamespace(vk=65)))

    def test_release_uses_original_press_character(self):
        events = []
        native = NativeInput(events.append, lambda: None, lambda: None)
        native.capturing = True
        native._physical_key(65, "char:a", True)
        native._physical_key(65, "char:A", False)
        self.assertEqual([event["key"] for event in events], ["char:a", "char:a"])

    def test_f8_never_forwarded_and_toggles_on_release(self):
        events, toggles = [], []
        native = NativeInput(events.append, lambda: toggles.append(True), lambda: None)
        native.capturing = True
        self.assertTrue(native._physical_key(119, "named:f8", True))
        self.assertTrue(native._physical_key(119, "named:f8", True))
        self.assertEqual(toggles, [])
        self.assertTrue(native._physical_key(119, "named:f8", False))
        self.assertEqual(toggles, [True])
        self.assertEqual(events, [])

    def test_emergency_immediately_releases_capture(self):
        calls = []
        native = NativeInput(lambda event: None, lambda: None, lambda: calls.append(True))
        native.capturing = True
        native._physical_key(1, "named:ctrl_l", True)
        native._physical_key(2, "named:alt_l", True)
        self.assertTrue(native._physical_key(3, "named:esc", True))
        self.assertFalse(native.capturing)
        self.assertEqual(calls, [True])

    def test_switch_refuses_held_buttons(self):
        native = NativeInput(lambda event: None, lambda: None, lambda: None)
        native._started = True
        native._physical_buttons.add("left")
        with self.assertRaisesRegex(RuntimeError, "松开"):
            native.set_capturing(True)
        self.assertFalse(native.capturing)

    def test_callback_failure_fails_open(self):
        def broken(event):
            raise RuntimeError("full queue")
        emergency = []
        native = NativeInput(broken, lambda: None, lambda: emergency.append(True))
        native.capturing = True
        native._emit({"type": "move", "dx": 1, "dy": 2})
        self.assertFalse(native.capturing)
        self.assertEqual(emergency, [True])

    def test_mac_tag_is_checked_before_callbacks(self):
        native = NativeInput(lambda event: None, lambda: None, lambda: None)
        native._quartz = types.SimpleNamespace(
            kCGEventSourceUserData=1,
            CGEventGetIntegerValueField=lambda event, field: event["tag"],
        )
        seen = []
        listener = types.SimpleNamespace(_handle_message=lambda *args: seen.append(args))
        native._guard_mac_callbacks(listener)
        # Even when the backend says injected=False, the tag takes precedence.
        listener._handle_message(None, 10, {"tag": native._tag}, None, False)
        self.assertEqual(seen, [])
        listener._handle_message(None, 10, {"tag": 0}, None, False)
        self.assertEqual(len(seen), 1)

    def test_failed_listener_start_is_reported(self):
        def failed_join(timeout=None):
            raise OSError("event tap unavailable")
        listener = types.SimpleNamespace(
            _condition=threading.Condition(), _ready=False,
            is_alive=lambda: False, join=failed_join)
        with self.assertRaisesRegex(RuntimeError, "event tap unavailable"):
            NativeInput._wait_until_ready(listener)

    def test_listener_start_wait_has_timeout(self):
        listener = types.SimpleNamespace(
            _condition=threading.Condition(), _ready=False, is_alive=lambda: True)
        with self.assertRaisesRegex(RuntimeError, "超时"):
            NativeInput._wait_until_ready(listener, timeout=0.01)


if __name__ == "__main__":
    unittest.main()
