"""Actual localhost TLS tests; no keyboard hooks or GUI required."""

import hashlib
import socket
import ssl
import struct
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

import kmlink.transport as transport_module
from kmlink.transport import (
    MAX_MESSAGE_BYTES, ProtocolError, Transport, _encode, _read_frame,
    _b64, _client_auth, _complete_handshake, _proof, _server_context, _transcript, _unb64,
    generate_pairing_key, normalize_pairing_key,
)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for transport state")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.transports = []

    def tearDown(self):
        for item in self.transports:
            item.stop()

    def transport(self):
        messages, statuses = [], []
        item = Transport(messages.append, lambda state, detail: statuses.append((state, detail)))
        self.transports.append(item)
        return item, messages, statuses

    def server(self, key):
        server, messages, statuses = self.transport()
        server.listen("127.0.0.1", 0, key)
        wait_until(lambda: server.listening_address is not None)
        return server, messages, statuses

    def test_pairing_key_round_trip(self):
        key = generate_pairing_key()
        self.assertEqual(len(key), 43)
        self.assertEqual(normalize_pairing_key(" \n" + key[:10] + " " + key[10:]), key)
        with self.assertRaises(ValueError):
            normalize_pairing_key("123456")

    def test_bidirectional_authenticated_tls_and_clean_stop(self):
        key = generate_pairing_key()
        server, server_messages, server_status = self.server(key)
        client, client_messages, client_status = self.transport()
        port = server.listening_address[1]
        client.connect("127.0.0.1", port, key)
        wait_until(lambda: server.is_connected and client.is_connected)
        self.assertTrue(client.send({"type": "text", "text": "你好，Mac 👋"}))
        self.assertTrue(server.send({"type": "move", "dx": 12, "dy": -3}))
        wait_until(lambda: bool(server_messages) and bool(client_messages))
        self.assertEqual(server_messages, [{"type": "text", "text": "你好，Mac 👋"}])
        self.assertEqual(client_messages, [{"type": "move", "dx": 12, "dy": -3}])
        self.assertFalse(client.send({"text": "x" * MAX_MESSAGE_BYTES}))
        client.stop()
        wait_until(lambda: not server.is_connected)
        self.assertFalse(client.send({"type": "key"}))
        server.stop()
        self.assertIsNone(server.listening_address)

    def test_wrong_key_never_delivers_then_correct_peer_can_join(self):
        key = generate_pairing_key()
        server, messages, statuses = self.server(key)
        bad, _, bad_status = self.transport()
        port = server.listening_address[1]
        bad.connect("127.0.0.1", port, generate_pairing_key())
        wait_until(lambda: any(state == "error" for state, _ in bad_status))
        self.assertFalse(bad.is_connected)
        self.assertFalse(messages)
        good, _, _ = self.transport()
        good.connect("127.0.0.1", port, key)
        wait_until(lambda: good.is_connected and server.is_connected)
        good.send({"type": "probe"})
        wait_until(lambda: bool(messages))
        self.assertEqual(messages, [{"type": "probe"}])

    def test_oversized_frame_rejected_before_reading_payload(self):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
            with self.assertRaises(ProtocolError):
                _read_frame(right, threading.Event())
        finally:
            left.close()
            right.close()

    def test_json_is_bounded_strict_object(self):
        for data in (b"[]", b'{"x":NaN}', b'{"x":1,"x":2}', b"\xff"):
            left, right = socket.socketpair()
            try:
                left.sendall(struct.pack("!I", len(data)) + data)
                with self.assertRaises(ProtocolError):
                    _read_frame(right, threading.Event())
            finally:
                left.close()
                right.close()
        with self.assertRaises(ProtocolError):
            _encode({"x": float("inf")})

    def test_stop_waiting_server_releases_port(self):
        server, _, _ = self.server(generate_pairing_key())
        port = server.listening_address[1]
        server.stop()
        other = socket.socket()
        try:
            other.bind(("127.0.0.1", port))
        finally:
            other.close()

    def test_unresponsive_authenticated_peer_times_out(self):
        key = generate_pairing_key()
        server, _, statuses = self.server(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(server.listening_address)
        peer = context.wrap_socket(raw, server_hostname=None)
        peer.settimeout(1)
        try:
            with patch.object(transport_module, "PEER_TIMEOUT", 0.2):
                _client_auth(peer, _unb64(key, 32), threading.Event())
                wait_until(lambda: server.is_connected)
                # This authenticated peer deliberately sends no heartbeat.
                wait_until(lambda: not server.is_connected)
                self.assertTrue(any(state == "disconnected" for state, _ in statuses))
        finally:
            peer.close()

    def test_server_proof_is_bound_to_tls_certificate(self):
        key = b"k" * 32
        left, right = socket.socketpair()
        failures = []

        class CertificateSocket:
            def sendall(self, data):
                return left.sendall(data)

            def recv(self, size):
                return left.recv(size)

            def getpeercert(self, binary_form=False):
                return b"actual TLS certificate"

        def bogus_server():
            try:
                hello = _read_frame(right, threading.Event())
                nonce = b"s" * 32
                # Valid proof for a different TLS endpoint must not pass.
                transcript = _transcript(
                    hashlib.sha256(b"another TLS certificate").digest(),
                    _unb64(hello["nonce"], 32), nonce,
                )
                right.sendall(_encode({
                    "version": 1, "nonce": _b64(nonce),
                    "proof": _proof(key, b"server", transcript),
                }))
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=bogus_server)
        thread.start()
        try:
            with self.assertRaises(ProtocolError):
                _client_auth(CertificateSocket(), key, threading.Event())
            thread.join(1)
            self.assertFalse(failures)
        finally:
            left.close()
            right.close()

    def test_stop_interrupts_an_incomplete_tls_handshake(self):
        server, _, _ = self.server(generate_pairing_key())
        peer = socket.create_connection(server.listening_address)
        try:
            wait_until(lambda: server._run.sock is not None)
            thread = server._run.thread
            server.stop()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            peer.close()


    def test_client_stop_interrupts_an_idle_tls_server(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        client, _, _ = self.transport()
        peer = None
        try:
            client.connect("127.0.0.1", listener.getsockname()[1], generate_pairing_key())
            peer, _ = listener.accept()
            wait_until(lambda: isinstance(client._run.sock, ssl.SSLSocket), timeout=2)
            thread = client._run.thread
            client.stop()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            if peer:
                peer.close()
            listener.close()


class TLSHandshakeTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory(prefix="kmlink-handshake-test-") as directory:
            context, _ = _server_context(directory)
        raw, self.peer = socket.socketpair()
        self.secure = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
        self.secure.settimeout(1.5)

    def tearDown(self):
        self.secure.close()
        self.peer.close()

    def test_idle_peer_has_one_bounded_handshake_deadline(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            _complete_handshake(self.secure, lambda: False, timeout=.12)
        self.assertLess(time.monotonic() - start, .75)
        self.assertEqual(self.secure.gettimeout(), 1.5)

    def test_event_cancels_handshake_without_closing_socket(self):
        stop = threading.Event()
        entered = threading.Event()
        failures = []

        def negotiate():
            entered.set()
            try:
                _complete_handshake(self.secure, stop, timeout=2)
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=negotiate, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(.5))
        time.sleep(.03)
        stop.set()
        worker.join(.75)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ConnectionAbortedError)
        self.assertEqual(self.secure.gettimeout(), 1.5)

    def test_cancel_racing_select_close_is_reported_as_cancelled(self):
        stop = threading.Event()

        def closed_during_poll(*args):
            stop.set()
            raise ValueError("file descriptor cannot be a negative integer")

        with patch.object(transport_module.select, "select", side_effect=closed_during_poll):
            with self.assertRaises(ConnectionAbortedError):
                _complete_handshake(self.secure, stop, timeout=1)


if __name__ == "__main__":
    unittest.main()
