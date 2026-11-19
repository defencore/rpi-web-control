# Recording triggers

A trigger holds a recording claim while its input is active. Several rules can hold claims simultaneously. The last release ends a recording once the manual recording control is also off. Rule IDs are stable identifiers across edits. Applying unrelated settings preserves existing claims and timer schedules. Removing, disabling, or editing an active rule releases its claim before its replacement starts.

Each rule has a unique `id` of 1–48 ASCII letters, digits, hyphens or underscores, a `type`, and an optional boolean `enabled` (default `true`). Up to 32 rules are supported. Unknown fields, invalid types and duplicate enabled GPIO or UART inputs are rejected before the active configuration changes.

| Type | Required fields | Optional fields and defaults |
| --- | --- | --- |
| `timer` | None | `interval_s: 60`, `duration_s: 5` |
| `gpio` | `pin` | `contact: "NO"`, `debounce_ms: 30` |
| `sensor` | `path`, `threshold` | `comparison: "above"`, `hysteresis: 0`, `poll_ms: 250` |
| `uart` | None | `device: "/dev/serial0"`, `baud: 115200`, `start_command: "START"`, `stop_command: "STOP"` |

## Timer

`{"id":"sample","type":"timer","interval_s":60,"duration_s":5}`

The first window opens one interval after enabling the rule. This example records for five seconds every minute. Schedules use the monotonic clock, skip elapsed windows, and restart when the service or rule restarts. Both values accept fractional seconds from 0.1 to 31,536,000. The duration must be shorter than the interval.

## GPIO

`{"id":"door","type":"gpio","pin":17,"contact":"NC","debounce_ms":30}`

Connect a voltage-free contact between the selected GPIO and ground. The service requests the RP1 line as an input with internal pull-up and watches both edges. Select the BCM GPIO number (0–27) exposed by the board or CM5 carrier. The driver discovers the chip by its kernel label, `pinctrl-rp1`. The service account needs access to the GPIO device.

| Contact | Normal state | Recording claim |
| --- | --- | --- |
| NO | Open, input high | Closed, input low |
| NC | Closed, input low | Open, input high |

The initial position is evaluated when the rule starts. A change must stay stable for `debounce_ms` (0–1000 ms) to qualify. A value of zero disables the filter. Short pulses can be lost if the kernel event buffer overflows during sustained load. Rule detection and camera startup have latency. A trigger starts capture on demand and has no pre-record buffer.

## Numeric sensor

`{"id":"temperature","type":"sensor","path":"/sys/class/thermal/thermal_zone0/temp","threshold":60000,"hysteresis":2000,"comparison":"above","poll_ms":250}`

Use a readable sysfs attribute containing one finite number in its native units. The thermal example starts at 60 °C and releases below 58 °C on drivers reporting millidegrees. An `above` rule activates at or above the threshold. A `below` rule activates at or below it and releases above the threshold plus hysteresis. The sampling interval is 20–60000 ms. Supported attribute trees are thermal and hwmon under `/sys/class`, plus IIO, 1-Wire and I2C devices under `/sys/bus`.

## UART

`{"id":"controller","type":"uart","device":"/dev/ttyAMA0","baud":115200,"start_command":"REC ON","stop_command":"REC OFF"}`

Choose the UART exposed by the carrier and configured in the OS. Serial aliases such as `/dev/serial0`, supported `ttyAMA`, `ttyS`, `ttyUSB`, `ttyACM` ports and `/dev/serial/by-id/...` are accepted. The service requests exclusive access, 8 data bits, no parity and 1 stop bit. Each message ends in LF or CRLF. Commands match exactly, including case and spaces. A start line holds the claim until a stop line, device failure or rule shutdown. Other lines are ignored. Command length is 1–64 printable ASCII characters. Use one rule per physical port, including its aliases.

## Runtime status

Each configured rule reports its ID, type, enabled setting, `active` claim, `state`, and last `error`. The state is `disabled`, `starting`, `ready`, or `error`. On an input failure the worker releases its claim and retries after two seconds. A reconnected UART needs a fresh start line. GPIO and sensor rules evaluate the current level again. Other workers continue independently.

To add an input, extend validation in `rpi_web_control/triggers.py`, register an adapter in `rpi_web_control/trigger_inputs.py`, and update the rule editor and its tests. Adapters own their device for the lifetime of one worker, call `ready()` once operational, call `publish(bool)` on level changes and check the stop event between bounded I/O waits.

## References

- [Raspberry Pi GPIO usage and chip discovery](https://pip.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-006553-WP/A-history-of-GPIO-usage-on-Raspberry-Pi-devices-and-current-best-practices.pdf)
- [libgpiod line requests](https://libgpiod.readthedocs.io/en/v2.3.2/python_line_request.html)
- [pySerial API](https://pyserial.readthedocs.io/en/stable/pyserial_api.html)
