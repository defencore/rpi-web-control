"""Handle one request on the systemd-accepted recording disk socket."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpi_web_control.nvme import NvmeManager
from rpi_web_control.nvme_client import serve_request

with socket.socket(fileno=sys.stdin.fileno()) as connection:
    serve_request(connection, NvmeManager)
