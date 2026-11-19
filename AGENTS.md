# rpi-web-control

This repository contains the network camera controller for Raspberry Pi 5 and Compute Module 5. Use English for UI text, comments, diagnostics, documentation, and project skills.

## Project skills

Read the relevant project skill before editing the corresponding subsystem.

| Work | Instructions |
| --- | --- |
| Camera modes, capture lifecycle, preview, RTP/JPEG, OSD, frame persistence, clock correlation, desktop rendering | [Camera development](skills/rpi-capture-development/SKILL.md) |
| Trigger adapters, web/API changes, storage devices, sensor boot profiles, Wi-Fi control, Debian packaging | [Appliance development](skills/rpi-appliance-development/SKILL.md) |

## Working agreement

- Keep hardware-specific imports inside the adapters so the application and its tests can run on a development machine.
- Preserve the timestamp unit, clock identity, and data type whenever a value crosses an interface. Include the clock name in a new field or in its documented schema.
- Use the simulator to test a complete user action through the same service and persistence paths used by the appliance. Label any simulator data in performance reports.
- Update the relevant skill when an interface, verification procedure, or hardware-specific constraint changes. Put operator instructions in the linked user documentation.
- The service targets the Pi 5 family. Record hardware results separately for each board, module, sensor mode, and destination tested. Follow [hardware validation](docs/hardware-validation.md) when a board is available.

## Local checks

Run `make test`, `make deb`, then `make verify-deb` at the repository root. Run `node --check` on each changed browser module and exercise the page at desktop and mobile widths. For an exported clip, use the [recording checker](skills/rpi-capture-development/scripts/check_recording.py) and the corresponding acceptance procedure.
