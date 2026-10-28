"""Two complete sessions over real TLS, with OS injection replaced by a recorder."""
import time
import unittest

from kmlink.engine import Bounds, Engine
from kmlink.native import validate_key_code
from kmlink.transport import generate_pairing_key


class InputRecorder:
    def __init__(self):
        self.xy = (100, 100)
        self.capturing = False
        self.keys = []
        self.buttons = []
        self.scrolls = []
        self.releases = 0

    def start(self): pass
    def stop(self): pass
    def position(self): return self.xy
    def set_capturing(self, capture): self.capturing = capture
    def move_to(self, x, y): self.xy = (x, y)
    def key(self, key, pressed): self.keys.append((key, pressed))
    def button(self, name, pressed, position=None): self.buttons.append((name, pressed))
    def scroll(self, dx, dy): self.scrolls.append((dx, dy))
    def release_all(self, position=None): self.releases += 1


class FullSessionTests(unittest.TestCase):
    def test_encrypted_session_input_clipboard_and_return(self):
        received_clipboard, states = [], []
        sender, receiver = InputRecorder(), InputRecorder()
        a = Engine(sender, Bounds(0, 0, 1920, 1080), lambda *v: states.append(v),
                   lambda text: None, validate_key_code)
        b = Engine(receiver, Bounds(0, 0, 1440, 900), lambda *v: states.append(v),
                   received_clipboard.append, validate_key_code)

        def wait_for(predicate, timeout=4):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                a.poll()
                b.poll()
                if predicate():
                    return
                time.sleep(.005)
            self.fail(f"Session timed out. States: {states}")

        key = generate_pairing_key()
        try:
            b.start("receiver", "127.0.0.1", 0, key, "right")
            wait_for(lambda: b.transport.listening_address is not None)
            port = b.transport.listening_address[1]
            a.start("controller", "127.0.0.1", port, key, "right")
            wait_for(lambda: a.remote_bounds is not None and b.remote_bounds is not None)
            a.enter_remote(1919, 540)
            wait_for(lambda: b.receiving and a.remote_active)
            self.assertTrue(sender.capturing)
            self.assertAlmostEqual(receiver.xy[1], 450, delta=1)
            for message in [
                {"type": "key", "key": "named:ctrl_l", "pressed": True},
                {"type": "key", "key": "char:c", "pressed": True},
                {"type": "key", "key": "char:c", "pressed": False},
                {"type": "key", "key": "named:ctrl_l", "pressed": False},
                {"type": "button", "button": "left", "pressed": True},
                {"type": "button", "button": "left", "pressed": False},
                {"type": "scroll", "dx": 0, "dy": .5},
                {"type": "scroll", "dx": 0, "dy": .5},
            ]:
                a.enqueue("input", message)
            wait_for(lambda: len(receiver.keys) == 4 and len(receiver.scrolls) == 1)
            self.assertEqual(receiver.keys[1], ("char:c", True))
            self.assertEqual(receiver.buttons, [("left", True), ("left", False)])
            self.assertEqual(receiver.scrolls, [(0, 1)])
            a.share_clipboard = b.share_clipboard = True
            a.clipboard_changed("跨平台剪贴板 ✓")
            wait_for(lambda: received_clipboard == ["跨平台剪贴板 ✓"])
            a.enqueue("input", {"type": "move", "dx": -50, "dy": 0})
            wait_for(lambda: not a.remote_active and not b.receiving)
            self.assertFalse(sender.capturing)
            self.assertGreater(receiver.releases, 0)
        finally:
            a.stop(announce=False)
            b.stop(announce=False)


if __name__ == "__main__":
    unittest.main()
