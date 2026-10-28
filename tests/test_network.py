"""Local adapter enumeration must never fall back to hostname resolution."""
import builtins
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kmlink.network import local_ipv4_addresses

try:
    from PySide6.QtNetwork import QNetworkInterface
    HAS_QT_NETWORK = True
except ImportError:
    HAS_QT_NETWORK = False


def address(text):
    return SimpleNamespace(toString=lambda: text)


class LocalAddressTests(unittest.TestCase):
    @unittest.skipUnless(HAS_QT_NETWORK, "QtNetwork is required for adapter tests")
    def test_filters_deduplicates_and_sorts_ipv4_without_dns(self):
        values = ["192.168.1.20", "10.2.0.3", "192.168.1.2", "192.168.1.20",
                  "127.0.0.1", "127.3.2.1", "0.0.0.0", "::1", "fe80::1%en0",
                  "::ffff:192.168.1.1", "239.255.77.77", "255.255.255.255",
                  "169.254.3.4", "invalid"]
        with patch.object(QNetworkInterface, "allAddresses", return_value=list(map(address, values))), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")), \
                patch("socket.gethostname", side_effect=AssertionError("Hostname must not be read")):
            self.assertEqual(local_ipv4_addresses(),
                             ["10.2.0.3", "169.254.3.4", "192.168.1.2", "192.168.1.20"])

    @unittest.skipUnless(HAS_QT_NETWORK, "QtNetwork is required for adapter tests")
    def test_enumeration_failure_returns_empty_without_dns(self):
        with patch.object(QNetworkInterface, "allAddresses", side_effect=OSError("adapter unavailable")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
            self.assertEqual(local_ipv4_addresses(), [])

    def test_missing_qt_returns_empty_without_dns(self):
        original_import = builtins.__import__

        def missing_qt(name, *args, **kwargs):
            if name == "PySide6.QtNetwork":
                raise ImportError("QtNetwork unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_qt), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
            self.assertEqual(local_ipv4_addresses(), [])

    def test_discovery_preserves_default_interface_without_hostname_lookup(self):
        from kmlink import discovery
        with patch.object(discovery, "local_ipv4_addresses", return_value=["10.0.0.2"]), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
            self.assertEqual(discovery._interface_addresses(), ["0.0.0.0", "10.0.0.2"])


if __name__ == "__main__":
    unittest.main()
