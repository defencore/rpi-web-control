"""IPv4 conflict detection for the configured Ethernet recovery address (RFC 5227)."""

import errno
import ipaddress
import random
import re
import socket
import struct
import time
from contextlib import contextmanager


ETHERNET = 1
IPV4 = 0x0800
ARP = 0x0806
BROADCAST = b"\xff" * 6
NO_MAC = b"\x00" * 6
NO_IP = b"\x00" * 4
PAYLOAD = struct.Struct("!HHBBH6s4s6s4s")


def _candidate(address):
    if not isinstance(address, (str, ipaddress.IPv4Address)):
        raise ValueError("Expected the configured recovery IPv4 address.")
    ip = ipaddress.IPv4Address(address)
    if (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.packed[0] == 0):
        raise ValueError("Use a unicast IPv4 address for Ethernet recovery.")
    return ip.packed


def _mac_bytes(own_mac):
    if isinstance(own_mac, str) and re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", own_mac):
        own_mac = bytes.fromhex(own_mac.replace(":", ""))
    if not isinstance(own_mac, bytes) or len(own_mac) != 6 or own_mac == NO_MAC or own_mac[0] & 1:
        raise ValueError("Expected the Ethernet interface's unicast MAC address.")
    return own_mac


@contextmanager
def _arp_link(ifname, own_mac):
    if not isinstance(ifname, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", ifname):
        raise ValueError("Invalid Ethernet interface name.")
    if not hasattr(socket, "AF_PACKET"):
        raise OSError(errno.EAFNOSUPPORT, "Ethernet address probing requires Linux packet sockets.")
    # A protocol of zero receives nothing until bind selects this interface and ARP.
    # SOCK_DGRAM lets Linux construct/check the link header and expose the ARP payload.
    with socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, 0) as link:
        link.bind((ifname, ARP))
        name, _, _, hardware_type, current_mac = link.getsockname()
        if name != ifname or hardware_type != ETHERNET or current_mac != own_mac:
            raise OSError(errno.ENODEV, "The recovery Ethernet interface changed.")
        yield link


def _send_request(link, ifname, own_mac, sender_ip, candidate):
    data = PAYLOAD.pack(ETHERNET, IPV4, 6, 4, 1, own_mac, sender_ip, NO_MAC, candidate)
    link.settimeout(1.0)
    if link.sendto(data, (ifname, ARP, 0, 0, BROADCAST)) != len(data):
        raise OSError(errno.EIO, "Incomplete recovery ARP request.")


def _conflict(payload, own_mac, candidate):
    if len(payload) < PAYLOAD.size:
        return False
    hardware, network, mac_size, ip_size, op, sender_mac, sender_ip, _, target_ip = PAYLOAD.unpack_from(payload)
    if ((hardware, network, mac_size, ip_size) != (ETHERNET, IPV4, 6, 4) or op not in (1, 2)
            or sender_mac in (own_mac, NO_MAC) or sender_mac[0] & 1):
        return False
    return sender_ip == candidate or (op == 1 and sender_ip == NO_IP and target_ip == candidate)


def _quiet(link, own_mac, candidate, seconds):
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        link.settimeout(remaining)
        try:
            data = link.recv(256)
        except TimeoutError:
            break
        if _conflict(data, own_mac, candidate):
            return False
    return True


def probe_address(ifname, address, own_mac):
    """Check one recovery address. A conflicting claim or simultaneous probe returns False."""
    candidate = _candidate(address)
    mac = _mac_bytes(own_mac)
    with _arp_link(ifname, mac) as link:
        if not _quiet(link, mac, candidate, random.uniform(0, 1)):
            return False
        for probe in range(3):
            _send_request(link, ifname, mac, NO_IP, candidate)
            delay = random.uniform(1, 2) if probe < 2 else 2
            if not _quiet(link, mac, candidate, delay):
                return False
    return True


def announce_address(ifname, address, own_mac):
    """Announce the configured recovery address once, after the caller has installed it."""
    candidate = _candidate(address)
    mac = _mac_bytes(own_mac)
    with _arp_link(ifname, mac) as link:
        _send_request(link, ifname, mac, candidate, candidate)
