"""Local adapter enumeration without hostname or DNS lookups."""
from __future__ import annotations

import ipaddress


def local_ipv4_addresses() -> list[str]:
    """Read OS interface addresses through Qt, including before QApplication.

    Resolving the machine's hostname can block for tens of seconds on systems
    whose hostname is not registered in DNS. An unavailable adapter API simply
    leaves the list empty; callers can retain the default-interface fallback.
    """
    try:
        from PySide6.QtNetwork import QNetworkInterface
        addresses = QNetworkInterface.allAddresses()
    except (ImportError, OSError):
        return []
    result = set()
    for item in addresses:
        try:
            address = ipaddress.IPv4Address(item.toString())
        except (AttributeError, ValueError):
            continue
        if (address.is_loopback or address.is_unspecified or address.is_multicast
                or int(address) == 0xFFFFFFFF):
            continue
        result.add(address)
    return [str(address) for address in sorted(result)]
