"""Entry point for the wired recovery address service."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpi_web_control.network_recovery import main

sys.exit(main())
