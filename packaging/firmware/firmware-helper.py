"""Handle one request on the privileged firmware socket."""

import sys

sys.path.insert(0, "/usr/lib/rpi-web-control")
from rpi_web_control.firmware import helper_main

helper_main()
