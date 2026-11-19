"""Entry point for the systemd network control socket service."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpi_web_control.network import helper_main

helper_main()
