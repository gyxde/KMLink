"""Presence validation and UDP lifecycle tests, without input hooks."""
import json
import socket
import time
import unittest
from unittest.mock import patch

import kmlink.discovery as discovery_module
from kmlink.discovery import Discovery, Peer, _Catalog, _RateLimit, _encode, _parse_packet


class PacketTests(unittest.TestCase):
    def packet(self, **overrides):
        value = {"app": "kmlink-discovery", "version": 1, "kind": "announce",
                 "device_id": "other-machine", "name": "书房 Mac", "system": "Darwin",
                 "port": 24882, "busy": False}
        value.update(overrides)
        return json.dumps(value, ensure_ascii=True).encode("utf-8")

    def test_packet_uses_actual_source_ip(self):
        kind, identity, peer = _parse_packet(self.packet(), "192.168.1.44", 10.)
        self.assertEqual((kind, identity), ("announce", "other-machine"))
        self.assertEqual(peer, Peer("other-machine", "书房 Mac", "Darwin", "192.168.1.44", 24882, False, 10.))
        with self.assertRaises(ValueError):
            _parse_packet(self.packet(ip="203.0.113.1"), "192.168.1.44", 10.)

    def test_rejects_malformed_or_unbounded_packets(self):
        cases = [b"", b"x" * 1401, b"\xff", b"[]", b'{"app":"x"}',
                 self.packet(version=True), self.packet(version=2), self.packet(port=True),
                 self.packet(port=0), self.packet(port=65536), self.packet(busy=1),
                 self.packet(name="evil\nname"), self.packet(system=" " * 5),
                 self.packet(name="evil\ud800"),
                 self.packet(device_id="short"), self.packet(kind="request-control"),
                 self.packet(name="X" * 81), self.packet(port=float("nan")),
                 b'{"app":"kmlink-discovery","version":1,"version":1}']
        for data in cases:
            with self.subTest(data=data[:80]), self.assertRaises((ValueError, UnicodeError)):
                _parse_packet(data, "192.168.1.44", 1.)

    def test_queries_have_no_device_details_or_secret(self):
        packet = _encode("query", "local-machine")
        self.assertEqual(set(json.loads(packet)), {"app", "version", "kind", "device_id"})
        self.assertEqual(_parse_packet(packet, "127.0.0.1", 1.)[:2], ("query", "local-machine"))

    def test_invalid_sender_address_rejected(self):
        for ip in ["0.0.0.0", "239.255.77.77", "255.255.255.255", "not-an-address"]:
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                _parse_packet(self.packet(), ip, 1.)


class CatalogTests(unittest.TestCase):
    def peer(self, identity="other-machine", **values):
        base = dict(device_id=identity, name="Mac", system="Darwin", ip="192.168.1.3",
                    port=24882, busy=False, last_seen=1.)
        base.update(values)
        return Peer(**base)

    def test_ignores_self_and_heartbeat_only_does_not_repaint(self):
        catalog = _Catalog("local-machine")
        self.assertFalse(catalog.update(self.peer("local-machine")))
        self.assertTrue(catalog.update(self.peer()))
        self.assertFalse(catalog.update(self.peer(last_seen=3.)))
        self.assertEqual(catalog.snapshot()[0].last_seen, 3.)
        self.assertTrue(catalog.update(self.peer(last_seen=4., busy=True)))

    def test_expiry_sorting_and_source_matched_goodbye(self):
        catalog = _Catalog("local-machine")
        catalog.update(self.peer("machine-b", name="beta"))
        catalog.update(self.peer("machine-a", name="Alpha", last_seen=4.))
        self.assertEqual([p.name for p in catalog.snapshot()], ["Alpha", "beta"])
        self.assertFalse(catalog.remove("machine-a", "192.168.1.99"))
        self.assertTrue(catalog.expire(8.))
        self.assertEqual([p.device_id for p in catalog.snapshot()], ["machine-a"])
        self.assertTrue(catalog.remove("machine-a", "192.168.1.3"))

    def test_catalog_and_traffic_have_memory_bounds(self):
        catalog = _Catalog("local-machine")
        for index in range(100):
            catalog.update(self.peer(f"machine-{index:03}"))
        self.assertEqual(len(catalog.snapshot()), discovery_module.MAX_PEERS)
        limiter = _RateLimit()
        self.assertEqual(sum(limiter.allow("192.168.1.1", 1.) for _ in range(100)), 24)
        self.assertTrue(limiter.allow("192.168.1.1", 2.))
        for index in range(500):
            limiter.allow(f"sender-{index}", 3.)
        self.assertLessEqual(len(limiter.sources), 256)

    def test_prefers_lan_over_tun_without_ip_flapping(self):
        catalog = _Catalog("local-machine")
        self.assertTrue(catalog.update(self.peer(ip="198.18.0.1", last_seen=1.)))
        self.assertTrue(catalog.update(self.peer(ip="192.168.1.5", last_seen=2.)))
        self.assertFalse(catalog.update(self.peer(ip="198.18.0.1", last_seen=3.)))
        self.assertFalse(catalog.update(self.peer(ip="10.1.2.3", last_seen=4.)))
        self.assertEqual(catalog.snapshot()[0].ip, "192.168.1.5")

    def test_expired_preferred_address_falls_back_to_fresh_candidate(self):
        catalog = _Catalog("local-machine")
        catalog.update(self.peer(ip="192.168.1.5", last_seen=1.))
        catalog.update(self.peer(ip="198.18.0.1", last_seen=5.))
        self.assertTrue(catalog.expire(8.))
        self.assertEqual(catalog.snapshot()[0].ip, "198.18.0.1")
        self.assertFalse(catalog.expire(10.))
        self.assertTrue(catalog.expire(12.))
        self.assertEqual(catalog.snapshot(), [])

    def test_goodbye_removes_only_matching_source_candidate(self):
        catalog = _Catalog("local-machine")
        catalog.update(self.peer(ip="192.168.1.5", last_seen=1.))
        catalog.update(self.peer(ip="198.18.0.1", last_seen=2.))
        self.assertFalse(catalog.remove("other-machine", "192.168.1.99"))
        self.assertFalse(catalog.remove("other-machine", "198.18.0.1"))
        self.assertEqual(catalog.snapshot()[0].ip, "192.168.1.5")
        catalog.update(self.peer(ip="198.18.0.1", last_seen=3.))
        self.assertTrue(catalog.remove("other-machine", "192.168.1.5"))
        self.assertEqual(catalog.snapshot()[0].ip, "198.18.0.1")

    def test_candidate_count_is_bounded_and_can_admit_better_lan(self):
        catalog = _Catalog("local-machine")
        for i in range(30):
            catalog.update(self.peer(ip=f"198.18.0.{i+1}", last_seen=1.))
        self.assertEqual(len(catalog._endpoints["other-machine"]), discovery_module.MAX_ADDRESSES_PER_PEER)
        catalog.update(self.peer(ip="192.168.1.5", last_seen=2.))
        self.assertEqual(catalog.snapshot()[0].ip, "192.168.1.5")
        self.assertEqual(len(catalog._endpoints["other-machine"]), discovery_module.MAX_ADDRESSES_PER_PEER)


class DiscoverySocketTests(unittest.TestCase):
    def test_real_udp_receives_then_stops_and_restarts_cleanly(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        changes, errors = [], []
        service = Discovery("local-machine", "Office PC", "Windows", 24882, changes.append, errors.append)
        with patch.object(discovery_module, "DISCOVERY_PORT", port), \
             patch.object(discovery_module, "_interface_addresses", return_value=["127.0.0.1"]), \
             patch.object(service, "_broadcast") as broadcast:
            try:
                service.start()
                service.start()
                self.wait_for(lambda: service.listening_address is not None)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.sendto(b"x" * 4096, ("127.0.0.1", port))
                    sender.sendto(_encode("announce", "remote-machine", "Remote Mac", "Darwin", 25000),
                                  ("127.0.0.1", port))
                    self.wait_for(lambda: bool(changes))
                    self.assertEqual(changes[-1][0].ip, "127.0.0.1")
                    sender.sendto(_encode("announce", "remote-machine", "Remote Mac", "Darwin", 25000, True),
                                  ("127.0.0.1", port))
                    self.wait_for(lambda: changes[-1][0].busy)
                service.stop()
                self.assertTrue(any(json.loads(call.args[1])["kind"] == "bye"
                                    for call in broadcast.call_args_list))
                self.assertEqual(service.peers, [])
                self.assertIsNone(service.listening_address)
                service.start()
                self.wait_for(lambda: service.listening_address is not None)
                self.assertFalse(errors)
            finally:
                service.stop()
                service.stop()

    @staticmethod
    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError("Discovery worker timed out")


if __name__ == "__main__":
    unittest.main()
