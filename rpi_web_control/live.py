"""Low-latency RFC 2435 JPEG transmission using the camera's exposure timestamps."""

import errno
import ipaddress
import re
import secrets
import socket
import struct
import threading
import time


_SEND_BUFFER = 256 * 1024
_SEND_PRESSURE = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}
_FRAME_RETRY_SECONDS = 0.015


def endpoint(host, port):
    try:
        if not isinstance(host, str):
            raise ValueError
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("Enter the receiver's IPv4 or IPv6 address.") from None
    if isinstance(address, ipaddress.IPv6Address):
        scope = address.scope_id
        if scope is not None and (not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", scope)
                                  or scope.isdecimal() and not 1 <= int(scope) <= 0xffffffff):
            raise ValueError("Enter a valid Pi interface name or index after the IPv6 scope sign (%).")
        if address.is_link_local and not scope:
            raise ValueError("Include the Pi interface for a link-local IPv6 receiver, for example fe80::1%eth0.")
        if address.ipv4_mapped:
            address = address.ipv4_mapped
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError("The live receiver must have a unicast address.")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Choose a UDP receiver port between 1024 and 65535.")
    return str(address), port


def check_geometry(width, height):
    if width % 8 or height % 8 or max(width, height) > 2040:
        raise ValueError("RTP/JPEG requires dimensions divisible by 8 and no larger than 2040 pixels.")


def validate_max_fps(max_fps):
    if max_fps is not None and (type(max_fps) is not int or not 1 <= max_fps <= 1000):
        raise ValueError("Choose a live FPS limit between 1 and 1000 or leave it unlimited.")
    return max_fps


def jpeg_scan(jpeg):
    # Both local encoders produce sequential JPEGs with the standard Huffman tables.
    # Read their actual quantizers; libjpeg quality values alone do not identify them.
    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("Incomplete JPEG from the live encoder.")
    tables = {}
    geometry = None
    offset = 2
    while offset + 4 <= len(jpeg) and jpeg[offset] == 0xff:
        marker = jpeg[offset + 1]
        size = int.from_bytes(jpeg[offset + 2:offset + 4], "big")
        end = offset + size + 2
        if size < 2 or end > len(jpeg):
            break
        segment = memoryview(jpeg)[offset + 4:end]
        if marker == 0xdb:
            if len(segment) % 65:
                raise ValueError("Unsupported JPEG quantization table precision.")
            for start in range(0, len(segment), 65):
                tables[segment[start]] = bytes(segment[start + 1:start + 65])
        elif marker == 0xc0:
            if (len(segment) != 15 or segment[0] != 8 or segment[5] != 3
                    or segment[7] not in (0x21, 0x22) or segment[8] != 0
                    or bytes(segment[10:12]) != b"\x11\x01" or bytes(segment[13:15]) != b"\x11\x01"):
                raise ValueError("The live encoder must produce baseline color JPEG in 4:2:0 or 4:2:2.")
            height, width = struct.unpack_from("!HH", segment, 1)
            check_geometry(width, height)
            geometry = bytes((int(segment[7] == 0x22), 255, width // 8, height // 8))
        elif marker == 0xdd:
            raise ValueError("The live encoder produced unexpected JPEG restart markers.")
        elif marker == 0xda:
            if geometry is None or set(tables) != {0, 1}:
                break
            return geometry, tables[0] + tables[1], memoryview(jpeg)[end:-2]
        offset = end
    raise ValueError("The live encoder produced an unsupported JPEG header.")


class RtpJpeg:
    def __init__(self):
        self.sequence = secrets.randbits(16)
        self.ssrc = secrets.randbits(32)
        self.time_offset = secrets.randbits(32)

    def packets(self, jpeg, sensor_ns):
        geometry, quantizers, scan = jpeg_scan(jpeg)
        timestamp = (self.time_offset + sensor_ns * 9 // 100000) & 0xffffffff
        quant_header = b"\x00\x00\x00\x80" + quantizers
        offset = 0
        while offset < len(scan):
            quant = quant_header if offset == 0 else b""
            end = min(len(scan), offset + 1200 - 20 - len(quant))
            payload_type = 26 | (0x80 if end == len(scan) else 0)
            header = struct.pack("!BBHII", 0x80, payload_type, self.sequence, timestamp, self.ssrc)
            self.sequence = (self.sequence + 1) & 0xffff
            yield header + offset.to_bytes(4, "big") + geometry + quant, scan[offset:end]
            offset = end


def idle_status():
    return {"enabled": False, "host": None, "port": 5004, "sent_frames": 0, "dropped_frames": 0,
            "queue_drops": 0, "congestion_drops": 0, "send_retries": 0, "sent_fps": 0.0,
            "max_fps": None, "rate_limited_frames": 0,
            "bitrate_mbps": 0.0, "last_error": None}


class LiveOutput:
    """One sender thread and one replaceable waiting frame, independent of the recorder."""

    def __init__(self, host, port, max_fps=None):
        self.destination = endpoint(host, port)
        self.max_fps = validate_max_fps(max_fps)
        self._interval_ns = 1_000_000_000 // max_fps if max_fps else 0
        self._last_sensor_ns = None
        self._next_frame_ns = 0
        # Resolve the numeric scope on the Pi once. IPv6 sendmsg needs the scope
        # index in its sockaddr as well as the receiver's address and port.
        try:
            family, _, _, _, self.sockaddr = socket.getaddrinfo(
                *self.destination, type=socket.SOCK_DGRAM, flags=socket.AI_NUMERICHOST)[0]
        except socket.gaierror:
            raise ValueError("Cannot use the receiver IP. For scoped IPv6, specify an interface on the Pi.") from None
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            self.sock.setblocking(False)
            if self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) < _SEND_BUFFER:
                try:
                    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SEND_BUFFER)
                except OSError:
                    pass  # Send within the existing kernel limit.
            self.rtp = RtpJpeg()
            self.condition = threading.Condition()
            self.pending = None
            self.running = True
            self.state = {**idle_status(), "host": self.destination[0], "port": port,
                          "enabled": True, "max_fps": self.max_fps}
            self.window = time.monotonic()
            self.window_bytes = self.window_frames = 0
            self.thread = threading.Thread(target=self._run, name="rtp-jpeg", daemon=True)
            self.thread.start()
        except BaseException:
            self.sock.close()
            raise

    def offer(self, frame, jpeg, osd):
        # One retained request permits asynchronous JPEG encoding without a frame copy.
        with self.condition:
            if not self.running:
                return
            sensor_ns = frame.sensor_ns
            if self._interval_ns:
                previous = self._last_sensor_ns
                self._last_sensor_ns = sensor_ns
                # Follow the sensor's cadence without accumulating timing jitter.
                # Start a fresh period after a clock reset or a missed whole period.
                if (previous is None or sensor_ns < previous
                        or sensor_ns >= self._next_frame_ns + self._interval_ns):
                    self._next_frame_ns = sensor_ns
                if sensor_ns < self._next_frame_ns:
                    self.state["rate_limited_frames"] += 1
                    return
                self._next_frame_ns += self._interval_ns
            if self.pending is not None:
                self._release_pending()
                self.state["dropped_frames"] += 1
                self.state["queue_drops"] += 1
            if jpeg is None:
                frame.acquire()
            self.pending = (sensor_ns, frame if jpeg is None else None, jpeg, osd)
            self.condition.notify()

    def _release_pending(self):
        if self.pending is not None:
            _, frame, _, _ = self.pending
            if frame is not None:
                frame.release()
            self.pending = None

    def status(self):
        with self.condition:
            return dict(self.state)

    def close(self):
        with self.condition:
            self.running = False
            self.state["enabled"] = False
            self._release_pending()
            self.condition.notify()
        self.thread.join()
        self.sock.close()
        with self.condition:
            self.state.update(sent_fps=0.0, bitrate_mbps=0.0)

    def _send_frame(self, jpeg, sensor_ns):
        sent = retries = 0
        retry_deadline = None
        sendmsg = getattr(self.sock, "sendmsg", None)
        for header, payload in self.rtp.packets(jpeg, sensor_ns):
            while True:
                try:
                    if sendmsg is not None:
                        sent += sendmsg([header, payload], [], 0, self.sockaddr)
                    else:
                        sent += self.sock.sendto(header + payload, self.sockaddr)
                    break
                except OSError as exc:
                    if exc.errno not in _SEND_PRESSURE:
                        return sent, retries, exc
                    now = time.monotonic()
                    if retry_deadline is None:
                        retry_deadline = now + _FRAME_RETRY_SECONDS
                    remaining = retry_deadline - now
                    if remaining <= 0:
                        return sent, retries, exc
                    # Some platforms report a writable UDP socket even on ENOBUFS.
                    # Back off instead of spinning, with one deadline for the whole JPEG.
                    time.sleep(min(0.001, remaining))
                    retries += 1
        return sent, retries, None

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: not self.running or self.pending is not None)
                if not self.running:
                    return
                sensor_ns, frame, jpeg, osd = self.pending
                self.pending = None
            byte_count = retries = 0
            error = None
            fatal = congested = False
            try:
                if frame is not None:
                    try:
                        jpeg = frame.live_jpeg(osd)
                    finally:
                        frame.release()
                byte_count, retries, send_error = self._send_frame(jpeg, sensor_ns)
                if send_error is not None:
                    congested = send_error.errno in _SEND_PRESSURE
                    if not congested:
                        error = str(send_error)
            except OSError as exc:
                error = f"Live frame encoding failed: {exc}"
            except Exception as exc:
                error, fatal = str(exc), True
            with self.condition:
                if not congested:
                    self.state["last_error"] = error
                self.state["send_retries"] += retries
                if error is None and not congested:
                    self.state["sent_frames"] += 1
                    self.window_frames += 1
                else:
                    self.state["dropped_frames"] += 1
                    if congested:
                        self.state["congestion_drops"] += 1
                self.window_bytes += byte_count
                now = time.monotonic()
                elapsed = now - self.window
                if elapsed >= 0.5:
                    self.state["sent_fps"] = round(self.window_frames / elapsed, 2)
                    self.state["bitrate_mbps"] = round(self.window_bytes * 8e-6 / elapsed, 2)
                    self.window, self.window_bytes, self.window_frames = now, 0, 0
                if fatal:
                    self.running = False
                    self.state["enabled"] = False
                    self._release_pending()
                    return
