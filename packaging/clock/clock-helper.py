"""Set CLOCK_REALTIME for one validated request on a systemd-accepted socket."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpi_web_control.device_clock import serve_request

with socket.socket(fileno=sys.stdin.fileno()) as connection:
    serve_request(connection)
