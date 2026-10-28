"""Local TLS invitation tests; no input hooks, GUI or global key events."""
import hashlib
import socket
import ssl
import struct
import threading
import time
import unittest
from unittest.mock import patch

import kmlink.pairing as module
from kmlink.pairing import (
    PairingService, PROTOCOL, MAX_FRAME, InvitationError, _b64, _commit,
    _frame, _read, comparison_code,
)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Invitation event did not arrive")


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.services = []

    def tearDown(self):
        for service, events in self.services:
            service.stop()

    def service(self, identity):
        events = []
        service = PairingService(identity, identity + " name", "Windows", lambda kind, data: events.append((kind, data)))
        self.services.append((service, events))
        service.start("127.0.0.1", 0)
        wait_for(lambda: service.port > 0)
        return service, events

    @staticmethod
    def event(events, kind):
        return next((data for name, data in events if name == kind), None)

    def pair(self, mode="control"):
        client, left = self.service("client")
        server, right = self.service("server")
        request_id = client.request("server", "127.0.0.1", server.port, mode)
        wait_for(lambda: self.event(left, "outgoing") and self.event(right, "incoming"))
        return client, left, server, right, request_id

    def test_approval_required_and_both_comparison_codes_match(self):
        with patch.object(module, "generate_pairing_key", wraps=module.generate_pairing_key) as generate:
            client, left, server, right, request_id = self.pair()
            outgoing, incoming = self.event(left, "outgoing"), self.event(right, "incoming")
            self.assertEqual(outgoing["code"], incoming["code"])
            self.assertRegex(outgoing["code"], r"^\d{6}$")
            self.assertEqual(outgoing["local_role"], "controller")
            self.assertEqual(incoming["local_role"], "receiver")
            self.assertEqual(incoming["peer_ip"], "127.0.0.1")
            self.assertNotIn("key", incoming)
            self.assertNotIn("key", outgoing)
            time.sleep(.05)
            generate.assert_not_called()
            self.assertIsNone(self.event(left, "connect_controller"))
            self.assertIsNone(self.event(right, "prepare_receiver"))
            # Calling approve on the requester must not approve the recipient.
            client.approve(request_id)
            time.sleep(.02)
            generate.assert_not_called()
            server.approve(request_id)
            wait_for(lambda: self.event(right, "prepare_receiver"))
            generate.assert_called_once()
            self.assertIsNone(self.event(left, "connect_controller"))
            server.receiver_ready(request_id, 24880)
            wait_for(lambda: self.event(left, "finished") and self.event(right, "finished"))
            receiver = self.event(right, "prepare_receiver")
            controller = self.event(left, "connect_controller")
            self.assertEqual(receiver["key"], controller["key"])
            self.assertEqual(controller["host"], "127.0.0.1")
            self.assertEqual(controller["port"], 24880)

    def test_invite_other_device_to_control_requester(self):
        client, left, server, right, request_id = self.pair("be_controlled")
        self.assertEqual(self.event(left, "outgoing")["local_role"], "receiver")
        self.assertEqual(self.event(right, "incoming")["local_role"], "controller")
        server.approve(request_id)
        wait_for(lambda: self.event(left, "prepare_receiver"))
        client.receiver_ready(request_id, 29999)
        wait_for(lambda: self.event(right, "connect_controller"))
        controller = self.event(right, "connect_controller")
        self.assertEqual(controller["host"], "127.0.0.1")
        self.assertEqual(controller["port"], 29999)
        self.assertEqual(controller["key"], self.event(left, "prepare_receiver")["key"])

    def test_decline_does_not_release_any_session_key(self):
        client, left, server, right, request_id = self.pair()
        server.reject(request_id)
        wait_for(lambda: self.event(left, "rejected") and self.event(right, "rejected"))
        self.assertFalse(any("key" in data for _, data in left + right))

    def test_busy_and_pending_requests_are_not_overwritten(self):
        client, left, server, right, request_id = self.pair()
        with self.assertRaises(RuntimeError):
            client.request("server", "127.0.0.1", server.port, "control")
        third, third_events = self.service("third")
        third.request("server", "127.0.0.1", server.port, "control")
        wait_for(lambda: self.event(third_events, "rejected"))
        self.assertEqual(len([1 for name, _ in right if name == "incoming"]), 1)
        self.assertEqual(server._run.pending.request_id, request_id)
        server.reject(request_id)

    def test_set_unavailable_refuses_without_confirmation(self):
        client, left = self.service("client")
        server, right = self.service("server")
        server.set_available(False)
        client.request("server", "127.0.0.1", server.port, "control")
        wait_for(lambda: self.event(left, "rejected"))
        self.assertIsNone(self.event(right, "incoming"))

    def test_cancel_while_waiting_and_retry(self):
        client, left, server, right, request_id = self.pair()
        client.cancel(request_id)
        wait_for(lambda: self.event(left, "rejected") and self.event(right, "rejected"))
        wait_for(lambda: client._run.pending is None and server._run.pending is None)
        second = client.request("server", "127.0.0.1", server.port, "control")
        self.assertNotEqual(second, request_id)
        wait_for(lambda: len([1 for kind, _ in right if kind == "incoming"]) == 2)
        server.reject(second)

    def test_cancel_during_receiver_preparation_suppresses_handoff(self):
        client, left, server, right, request_id = self.pair()
        server.approve(request_id)
        wait_for(lambda: self.event(right, "prepare_receiver"))
        client.cancel(request_id)
        wait_for(lambda: self.event(right, "rejected"))
        server.receiver_ready(request_id, 24880)
        time.sleep(.05)
        self.assertIsNone(self.event(left, "connect_controller"))
        self.assertIsNone(self.event(left, "finished"))

    def test_approval_timeout_releases_pending_slot(self):
        with patch.object(module, "APPROVAL_TIMEOUT", .12):
            client, left, server, right, request_id = self.pair()
            wait_for(lambda: self.event(left, "rejected") and self.event(right, "rejected"))
            wait_for(lambda: client._run.pending is None and server._run.pending is None)
            self.assertFalse(any("key" in data for _, data in left + right))

    def test_ready_timeout_does_not_connect(self):
        with patch.object(module, "READY_TIMEOUT", .12):
            client, left, server, right, request_id = self.pair()
            server.approve(request_id)
            wait_for(lambda: self.event(right, "prepare_receiver"))
            wait_for(lambda: self.event(left, "rejected") and self.event(right, "rejected"))
            self.assertIsNone(self.event(left, "connect_controller"))

    def test_stop_interrupts_consent_without_late_events(self):
        client, left, server, right, request_id = self.pair()
        count = len(right)
        server.stop()
        server.approve(request_id)
        server.receiver_ready(request_id, 24880)
        wait_for(lambda: self.event(left, "rejected"))
        self.assertEqual(len(right), count)
        self.assertIsNone(self.event(left, "connect_controller"))

    def test_target_id_mismatch_never_prompts_recipient(self):
        client, left = self.service("client")
        server, right = self.service("server")
        client.request("wrong-id", "127.0.0.1", server.port, "control")
        wait_for(lambda: self.event(left, "rejected") or self.event(left, "error"))
        self.assertIsNone(self.event(right, "incoming"))

    def test_commitment_mismatch_is_rejected_before_consent(self):
        server, events = self.service("server")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", server.port)) as raw:
            with context.wrap_socket(raw) as secure:
                secure.settimeout(.3)
                request = {"type": "request", "protocol": PROTOCOL, "request_id": "a" * 32,
                           "target_id": "server", "device": {"id": "client", "name": "Client", "system": "Darwin"},
                           "mode": "control", "nonce_commit": _commit(b"a" * 32)}
                secure.sendall(_frame(request))
                challenge = _read(secure, lambda: False)
                self.assertEqual(challenge["type"], "challenge")
                self.assertNotIn("nonce", challenge)
                secure.sendall(_frame({"type": "reveal", "nonce": _b64(b"b" * 32)}))
                self.assertEqual(secure.recv(1), b"")
        self.assertIsNone(self.event(events, "incoming"))
        self.assertFalse(any("key" in data for _, data in events))

    def test_comparison_binds_certificate_roles_and_identities(self):
        request = {"id": "client", "target_id": "server", "mode": "control"}
        challenge = {"id": "server"}
        expected = comparison_code(b"a" * 32, request, challenge, b"b" * 32, b"c" * 32)
        variants = [comparison_code(b"z" * 32, request, challenge, b"b" * 32, b"c" * 32),
                    comparison_code(b"a" * 32, {**request, "mode": "be_controlled"}, challenge, b"b" * 32, b"c" * 32),
                    comparison_code(b"a" * 32, request, {"id": "imposter"}, b"b" * 32, b"c" * 32)]
        self.assertTrue(all(item != expected for item in variants))

    def test_bounded_strict_frames(self):
        for payload in (b"[]", b'{"x":1,"x":2}', b'{"x":NaN}'):
            left, right = socket.socketpair()
            try:
                left.sendall(struct.pack("!I", len(payload)) + payload)
                with self.assertRaises(InvitationError):
                    _read(right, lambda: False)
            finally:
                left.close(); right.close()
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!I", MAX_FRAME + 1))
            with self.assertRaises(InvitationError):
                _read(right, lambda: False)
        finally:
            left.close(); right.close()

    def test_identity_rejects_hidden_controls_and_surrogates(self):
        for name in (" leading", "trailing ", "line\nbreak", "\u202espoof", "\u2028", "\ud800"):
            with self.subTest(name=repr(name)), self.assertRaises(InvitationError):
                PairingService("device", name, "Windows", lambda *_: None)


    def test_stop_interrupts_idle_incoming_tls_handshake(self):
        server, _ = self.service("server")
        peer = socket.create_connection(("127.0.0.1", server.port), timeout=1)
        try:
            wait_for(lambda: bool(server._run.workers))
            workers = list(server._run.workers)
            wait_for(lambda: any(isinstance(sock, ssl.SSLSocket) for sock in server._run.sockets))
            server.stop()
            for worker in workers:
                worker.join(.5)
                self.assertFalse(worker.is_alive())
        finally:
            peer.close()

    def test_cancel_interrupts_idle_outgoing_tls_handshake(self):
        client, events = self.service("client")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1)
        peer = None
        try:
            request_id = client.request("server", "127.0.0.1", listener.getsockname()[1], "control")
            peer, _ = listener.accept()
            wait_for(lambda: client._run.pending is not None and isinstance(client._run.pending.sock, ssl.SSLSocket))
            workers = list(client._run.workers)
            client.cancel(request_id)
            wait_for(lambda: client._run.pending is None)
            for worker in workers:
                worker.join(.5)
                self.assertFalse(worker.is_alive())
            self.assertIsNotNone(self.event(events, "rejected"))
        finally:
            if peer:
                peer.close()
            listener.close()


if __name__ == "__main__":
    unittest.main()
