"""Discovery UI and consent lifecycle, without network or native input hooks."""
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPushButton
    from kmlink.app import MainWindow
    from kmlink.discovery import Peer
    from kmlink.pairing import PairingService
    HAS_QT = True
except ImportError:
    HAS_QT = False


class FakePairing:
    def __init__(self):
        self.calls = []

    def request(self, peer_id, host, port, mode):
        self.calls.append(("request", peer_id, host, port, mode))
        return "request-one"

    def approve(self, request_id): self.calls.append(("approve", request_id))
    def reject(self, request_id): self.calls.append(("reject", request_id))
    def cancel(self, request_id): self.calls.append(("cancel", request_id))
    def receiver_ready(self, request_id, port): self.calls.append(("ready", request_id, port))
    def set_available(self, value): self.calls.append(("available", value))
    def stop(self): self.calls.append(("stop",))


class FakeDiscovery:
    def __init__(self): self.calls = []
    def set_busy(self, value): self.calls.append(("busy", value))
    def refresh(self): self.calls.append(("refresh",))
    def stop(self): self.calls.append(("stop",))


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.share_clipboard = False
        self.map_ctrl_cmd = False
        self.remote_bounds = None
        self.transport = SimpleNamespace(listening_address=("127.0.0.1", 24880))

    def start(self, *args): self.calls.append(("start", *args))
    def stop(self, **kwargs): self.calls.append(("stop", kwargs))
    def poll(self): pass


class NativeRecorder:
    """Provide a real Engine with no access to the desktop input subsystem."""
    def __init__(self):
        self.capturing = False
        self.xy = (100, 100)
        self.starts = 0
        self.stops = 0
        self.releases = 0

    def start(self): self.starts += 1
    def stop(self): self.stops += 1
    def set_capturing(self, value): self.capturing = value
    def release_all(self): self.releases += 1
    def position(self): return self.xy
    def move_to(self, x, y): self.xy = (x, y)
    def key(self, key, pressed): pass
    def button(self, name, pressed): pass
    def scroll(self, dx, dy): pass


@unittest.skipUnless(HAS_QT, "PySide6 is required for UI lifecycle tests")
class DiscoveryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(enable_discovery=False)
        self.window.timer.stop()
        self.window.clipboard_timer.stop()
        self.pairing = FakePairing()
        self.discovery = FakeDiscovery()
        self.engine = FakeEngine()
        self.window.pairing = self.pairing
        self.window.discovery = self.discovery
        self.window.engine = self.engine
        self.permissions = patch("kmlink.app.permissions_status", return_value="")
        self.permissions.start()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.permissions.stop()

    def peer(self, peer_id="remote-one", busy=False):
        return Peer(peer_id, "Office Mac", "Darwin", "192.168.1.22", 41001, busy, time.monotonic())

    def invitation(self, local_role="receiver"):
        return {"request_id": "request-one", "peer_id": "remote-one", "peer_name": "Office Mac",
                "peer_system": "Darwin", "peer_ip": "192.168.1.22", "local_role": local_role,
                "mode": "control", "code": "042317", "key": "test-session-key",
                "host": "192.168.1.22", "port": 24880}

    def test_device_rows_show_metadata_and_busy_disables_actions(self):
        self.window._render_peers([self.peer()])
        table = self.window.peer_table
        self.assertEqual([table.item(0, i).text() for i in range(4)],
                         ["Office Mac", "Darwin", "192.168.1.22", "可连接"])
        self.assertTrue(self.window.control_button.isEnabled())
        self.assertTrue(self.window.invite_button.isEnabled())
        self.window._render_peers([self.peer(busy=True)])
        self.assertEqual(table.item(0, 3).text(), "使用中")
        self.assertFalse(self.window.control_button.isEnabled())
        self.assertFalse(self.window.invite_button.isEnabled())

    def test_selection_survives_list_refresh_and_disappearing_peer_clears_it(self):
        other = Peer("remote-two", "Windows PC", "Windows", "192.168.1.23", 41002, False, time.monotonic())
        self.window._render_peers([self.peer(), other])
        self.window.peer_table.selectRow(1)
        self.window._render_peers([other, self.peer()])
        self.assertEqual(self.window._selected_peer().device_id, "remote-two")
        self.window._render_peers([])
        self.assertIsNone(self.window._selected_peer())
        self.assertFalse(self.window.control_button.isEnabled())

    def test_both_request_directions_wait_for_consent_without_starting_engine(self):
        for mode in ("control", "be_controlled"):
            with self.subTest(mode=mode):
                self.window._render_peers([self.peer()])
                self.window._request_control(mode)
                self.assertIn(("request", "remote-one", "192.168.1.22", 41001, mode), self.pairing.calls)
                self.assertEqual(self.engine.calls, [])
                self.assertFalse(self.window.control_button.isEnabled())
                self.assertIn(("available", False), self.pairing.calls)
                self.window._cancel_pair("request-one")
                self.assertIsNone(self.window._request_id)

    def test_incoming_dialog_defaults_to_reject_and_approval_does_not_start_input(self):
        data = self.invitation()
        self.window._pair_event("incoming", data)
        self.assertEqual(self.engine.calls, [])
        defaults = [b.text() for b in self.window._pair_dialog.findChildren(QPushButton) if b.isDefault()]
        self.assertEqual(defaults, ["拒绝"])
        self.window._approve_pair(data["request_id"])
        self.assertIn(("approve", data["request_id"]), self.pairing.calls)
        self.assertEqual(self.engine.calls, [])

    def test_receiver_announces_ready_only_after_engine_listener_binds(self):
        self.window._request_id = "request-one"
        self.window._pair_event("prepare_receiver", self.invitation())
        self.assertEqual(self.engine.calls[0][:3], ("start", "receiver", "0.0.0.0"))
        self.assertFalse(any(call[0] == "ready" for call in self.pairing.calls))
        self.window._changed("listening", "bound")
        self.assertIn(("ready", "request-one", 24880), self.pairing.calls)

    def test_abandoned_receiver_handoff_expires_after_invitation_finishes(self):
        self.window._request_id = "request-one"
        self.window._pair_event("prepare_receiver", self.invitation())
        self.assertGreater(self.window._auto_receiver_deadline, time.monotonic())
        self.window._pair_event("finished", {"request_id": "request-one"})
        self.assertIsNone(self.window._request_id)
        self.assertTrue(self.window._active)
        self.window._auto_receiver_deadline = time.monotonic() - 1
        self.window._poll()
        self.assertTrue(any(call[0] == "stop" for call in self.engine.calls))
        self.assertFalse(self.window._active)
        self.assertEqual(self.window._auto_receiver_deadline, 0)
        self.assertIn("对方未完成连接", self.window.detail.text())

    def test_ready_receiver_clears_handoff_deadline(self):
        self.window._request_id = "request-one"
        self.window._pair_event("prepare_receiver", self.invitation())
        self.window._pair_event("finished", {"request_id": "request-one"})
        self.window._changed("ready", "connected")
        self.assertEqual(self.window._auto_receiver_deadline, 0)
        with patch("kmlink.app.time.monotonic", return_value=time.monotonic() + 60):
            self.window._poll()
        self.assertFalse(any(call[0] == "stop" for call in self.engine.calls))
        self.assertTrue(self.window._active)

    def test_stale_handoff_after_cancellation_never_starts_engine(self):
        self.window._request_id = "request-one"
        self.window._cancel_pair("request-one")
        self.window._pair_event("prepare_receiver", self.invitation())
        self.window._pair_event("connect_controller", self.invitation("controller"))
        self.assertFalse(any(call[0] == "start" for call in self.engine.calls))

    def test_cancelled_or_unknown_outgoing_event_does_not_reopen_dialog(self):
        self.window._request_id = "request-one"
        self.window._cancel_pair("request-one")
        self.window._pair_event("outgoing", self.invitation())
        self.window._pair_event("incoming", self.invitation())
        unknown = {**self.invitation(), "request_id": "never-requested"}
        self.window._pair_event("outgoing", unknown)
        self.assertIsNone(self.window._pair_dialog)
        self.assertIsNone(self.window._request_id)
        self.assertFalse(self.window._active)

    def test_old_generation_listening_is_ignored_after_service_stop(self):
        old_generation = self.window._service_generation
        self.window._queue_service("pairing", "listening", {"port": 41001}, old_generation)
        self.window._stop_services()
        # Even if a replacement service now exists, the previous worker's
        # queued or late callback must not start its discovery service.
        self.window.pairing = FakePairing()
        self.window._queue_service("pairing", "listening", {"port": 41001}, old_generation)
        with patch("kmlink.app.Discovery") as create_discovery:
            self.window._poll()
        create_discovery.assert_not_called()
        self.assertIsNone(self.window.discovery)
        self.assertTrue(self.window._service_events.empty())

    def test_overflow_drains_old_listening_events_without_restarting_discovery(self):
        old_generation = self.window._service_generation
        for _ in range(self.window._service_events.maxsize + 1):
            self.window._queue_service("pairing", "listening", {"port": 41001}, old_generation)
        self.assertTrue(self.window._service_overflow.is_set())
        with patch("kmlink.app.Discovery") as create_discovery:
            self.window._poll()
        create_discovery.assert_not_called()
        self.assertIsNone(self.window.pairing)
        self.assertIsNone(self.window.discovery)
        self.assertTrue(self.window._service_events.empty())
        self.assertFalse(self.window._service_overflow.is_set())
        self.assertGreater(self.window._service_generation, old_generation)
        self.assertTrue(any(call[0] == "stop" for call in self.engine.calls))
        self.window._queue_service("pairing", "listening", {"port": 41001}, old_generation)
        self.assertTrue(self.window._service_events.empty())

    def test_cancel_after_accept_stops_controller_handoff(self):
        self.window._request_id = "request-one"
        self.window._pair_event("connect_controller", self.invitation("controller"))
        self.window._cancel_pair("request-one", incoming=True)
        self.assertIn(("cancel", "request-one"), self.pairing.calls)
        self.assertTrue(any(call[0] == "stop" for call in self.engine.calls))
        self.assertFalse(self.window._active)

    def test_failed_invitation_stops_controller_handoff(self):
        self.window._request_id = "request-one"
        self.window._pair_event("connect_controller", self.invitation("controller"))
        self.window._pair_event("error", {"request_id": "request-one", "detail": "cancelled"})
        self.assertTrue(any(call[0] == "stop" for call in self.engine.calls))
        self.assertFalse(self.window._active)

    def test_close_stops_network_and_input(self):
        self.window.close()
        self.assertTrue(self.window._closing)
        self.assertIn(("stop",), self.pairing.calls)
        self.assertIn(("stop",), self.discovery.calls)
        self.assertTrue(any(call[0] == "stop" for call in self.engine.calls))

    def _real_tls_handoff(self, mode):
        """Exercise UI consent, invitation TLS, and control TLS together."""
        from kmlink.engine import Engine
        from kmlink.native import validate_key_code

        first = self.window
        second = MainWindow(enable_discovery=False)
        second.timer.stop()
        second.clipboard_timer.stop()
        events = {"first": [], "second": []}
        try:
            for title, window in (("first", first), ("second", second)):
                native = NativeRecorder()
                window.native = native
                window.engine = Engine(native, window.engine.bounds if hasattr(window.engine, "bounds")
                                       else second.engine.bounds, window._changed, lambda text: None,
                                       validate_key_code)
                # The data listener may ask the OS for an unused port. The
                # handoff must advertise the bound port, not the spinner value.
                window.port.setRange(0, 65535)
                window.port.setValue(0)
                original = window._pair_event
                window._pair_event = lambda kind, data, original=original: (
                    original(kind, data) if kind != "listening" else None)

                def event(kind, data, title=title, window=window):
                    events[title].append((kind, data))
                    window._queue_service("pairing", kind, data)

                window.pairing = PairingService(window._device_id, title, "Test OS", event)
                window.pairing.start("127.0.0.1", 0)

            def wait_for(predicate, timeout=5):
                until = time.monotonic() + timeout
                while time.monotonic() < until:
                    self.app.processEvents()
                    first._poll()
                    second._poll()
                    if predicate():
                        return
                    time.sleep(.005)
                summary = {name: [kind for kind, _ in history] for name, history in events.items()}
                self.fail(f"UI/TLS handoff timed out: {summary}")

            wait_for(lambda: first.pairing.port != 0 and second.pairing.port != 0)
            first._render_peers([Peer(second._device_id, "Second device", "Test OS", "127.0.0.1",
                                      second.pairing.port, False, time.monotonic())])
            first._request_control(mode)
            wait_for(lambda: first._pair_dialog is not None and second._pair_dialog is not None)
            outgoing = next(data for kind, data in events["first"] if kind == "outgoing")
            incoming = next(data for kind, data in events["second"] if kind == "incoming")
            self.assertEqual(outgoing["code"], incoming["code"])
            self.assertEqual(first.native.starts, 0)
            self.assertEqual(second.native.starts, 0)
            second._approve_pair(second._request_id)
            wait_for(lambda: first.engine.remote_bounds is not None and second.engine.remote_bounds is not None)
            roles = ("controller", "receiver") if mode == "control" else ("receiver", "controller")
            self.assertEqual((first.engine.role, second.engine.role), roles)
            self.assertTrue(first.engine.transport.is_connected)
            self.assertTrue(second.engine.transport.is_connected)
            self.assertTrue(first._active and second._active)
            self.assertEqual((first.native.starts, second.native.starts), (1, 1))
            wait_for(lambda: first._request_id is None and second._request_id is None)
            first._stop()
            wait_for(lambda: not second.engine.connected)
            self.assertGreater(first.native.releases, 0)
            self.assertGreater(second.native.releases, 0)
        finally:
            second.close()
            first._stop()

    def test_real_tls_ui_handoff_for_control_request(self):
        self._real_tls_handoff("control")

    def test_real_tls_ui_handoff_for_be_controlled_invitation(self):
        self._real_tls_handoff("be_controlled")


if __name__ == "__main__":
    unittest.main()
