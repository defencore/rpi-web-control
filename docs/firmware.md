# Sensor profiles and Raspberry Pi boot configuration

The Camera page shows the sensor detected by libcamera, its available video modes, and the firmware settings for the next startup. Open the firmware panel to read the current `/boot/firmware/config.txt`. A staged profile starts taking effect when the board boots. Verify the detected sensor again after reconnecting.

## Switch a profile

1. Open **Camera** and refresh the firmware panel. Find the sensor under **Installed overlays** or **Missing overlays**. Each group is sorted alphabetically. Check the current file and selected CSI connector.
2. Choose the sensor fitted to the board and the connector routed by the carrier. CAM0 produces an overlay with the `,cam0` option. CAM1 uses the overlay's default connector. For modules with multiple oscillator or lane options, use the module vendor's specification. For a CM5, use an explicit camera profile. The automatic option uses firmware detection for supported official camera modules on Raspberry Pi SBCs.
3. Select **Stage profile**. The panel displays the resulting file and marks the change **Reboot pending**. If the menu reports **overlay missing**, install the matching compiled overlay and compatible sensor driver for the board's kernel, then refresh the panel.
4. Stop active recordings and release their trigger claims. Select **Reboot device…** and confirm. The management connection closes as the OS restarts. Reconnect to the device and check the model and sensor modes before enabling preview or recording. If the physical camera needs changing, power the board down before changing its ribbon connection.
5. If the new sensor fails to enumerate, open the firmware panel, restore its previous configuration, and reboot to load that configuration. A single undo is available for the last successful profile change while the current file still matches the saved revision.

For example, Camera Module 3 on CAM0 produces this block in the unconditional section.

```ini
[all]
# BEGIN rpi-web-control camera profile
camera_auto_detect=0
dtoverlay=imx708,cam0
# END rpi-web-control camera profile
```

## OV9281 and four-lane IMX577 on CAM0

The **Sensor boot profile** menu includes both OV9281 and IMX577. Select the sensor fitted to CAM0, set **CSI connector** to **CAM0**, and review the directive preview before selecting **Stage profile**. Each choice writes `camera_auto_detect=0` and the corresponding directive under `[all]`.

| Profile | CAM0 directive |
| --- | --- |
| OV9281 | `dtoverlay=ov9281,cam0` |
| IMX577, four lanes at 750 MHz link frequency | `dtoverlay=imx577,cam0,4lane,link-frequency=750000000` |

Both profiles stay in the menu when you switch. If the existing top-level boot file already contains both CAM0 directives in its global section, the panel shows **Choose a profile**. Select the fitted sensor, check the preview, and choose **Stage profile**. The helper backs up the original file and replaces the two directives with a single managed overlay for that sensor. **Restore backup** restores the original file, including both directives. Lines beginning with `#`, such as `# profile OV9281`, are comments.

The IMX577 profile requires four CSI lanes on the connector and an installed `imx577.dtbo` and driver that support `4lane` and `link-frequency=750000000`. OV9281 requires `ov9281.dtbo` and its matching driver. The menu checks for the overlay file on the boot volume. After reboot, use the detected sensor model and mode list to verify the hardware and driver combination.

## Built-in camera profiles

The automatic profile sets `camera_auto_detect=1`. Raspberry Pi Camera Module 2 uses `imx219` for both its IMX219 and IMX355 versions. Camera Modules 1 and 3 use `ov5647` and `imx708`, the High Quality Camera uses `imx477` and the Global Shutter Camera uses `imx296`. IMX378 and the IMX290/327 module variants that use the `imx290` overlay remain available. [Raspberry Pi camera configuration](https://www.raspberrypi.com/documentation/accessories/camera-software.html#configuration)

The table gives the value after `dtoverlay=` for CAM1. Selecting CAM0 adds the connector option to the preview. **Installed overlays** indicates that the overlay file exists on the boot volume. After reboot, check the detected model and the modes reported by libcamera to verify that the driver works with the module. [Raspberry Pi overlay reference](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)

| Module | CAM1 overlay and parameters | How to choose |
| --- | --- | --- |
| Raspberry Pi AI Camera | `imx500` | On Pi 5 and CM5 the firmware overlay map selects `imx500-pi5.dtbo`. The panel checks for this file and `overlay_map.dtb`. Install `imx500-all` for the sensor's runtime firmware. [AI Camera setup](https://www.raspberrypi.com/documentation/accessories/ai-camera.html) |
| Arducam 16 MP IMX519 | `imx519` or `imx519,vcm=off` | Choose the first for autofocus and the second for fixed-focus hardware. Arducam specifies its libcamera software for autofocus control. [IMX519 guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/16MP-IMX519/) |
| Arducam 64 MP Hawkeye | `arducam-64mp` | Use the driver and matching camera software for the Hawkeye. [Hawkeye guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/64MP-Hawkeye/) |
| Arducam 64 MP OwlSight | `ov64a40,link-frequency=360000000` or `ov64a40,link-frequency=456000000` | Arducam recommends 360 MHz for stability and offers 456 MHz for higher link speed. [OwlSight guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/64MP-OV64A40/) |
| Arducam Pivariety carrier | `arducam-pivariety` | Choose this for boards using the Pivariety driver, including the B0381 OV2311 module. [Pivariety guide](https://docs.arducam.com/Raspberry-Pi-Camera/Pivariety-Camera/Quick-Start-Guide/) |
| Native CSI modules | `imx258`, `ov2311`, `ov7251` | Match the device name to the native sensor driver on the Pi. |
| Sony IMX283 | `imx283` | Requires four CSI lanes on the connector, carrier and cable. |
| Sony IMX335 | `imx335` or `imx335,4lane` | Use the default for the Arducam B0568. Choose the four-lane variant for modules and wiring that support four lanes. [IMX335 guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/5MP-IMX335/) |
| Sony IMX415 | `imx415` | Uses the installed overlay's defaults. If the module needs a different I²C address, oscillator, link frequency or lane setting, add a vendor profile with those parameters. |
| Sony IMX327 | `imx327` or `imx327,clock-frequency=74250000` | Choose the module's oscillator frequency. The default is 37.125 MHz and the second profile is 74.25 MHz. |
| Sony IMX462 | `imx462` or `imx462,clock-frequency=74250000` | The same 37.125 MHz and 74.25 MHz options apply to the native IMX462 overlay. |

## Add a vendor profile without rebuilding

Install the vendor's compatible kernel driver and overlay on the target. Create an optional JSON list at `/etc/rpi-web-control/sensor-profiles.json`, owned by root. Use file permissions such as `0644`. The API reads it again when the firmware panel reloads. Only the listed IDs can be selected over the network.

The following is a schema example. Replace the example overlay and parameters with names supported by the installed driver.

```json
[
  {
    "id": "vendor-fast",
    "label": "Vendor high-speed sensor",
    "overlay": "vendor-module",
    "params": ["rotation=180", "fast-mode"]
  }
]
```

`id` starts with a lowercase letter and can include lowercase letters, digits and hyphens, up to 48 characters. Choose a unique ID distinct from the built-ins and `restore`. `label` contains up to 80 printable ASCII characters. `overlay` is the basename of a DT overlay, without a path or the `.dtbo` extension. For this example, the helper checks for `/boot/firmware/overlays/vendor-module.dtbo`. Optional `params` holds flags or `name=value` strings. The connector comes from the panel. A full overlay line must fit the firmware's 98-character limit. A malformed catalog leaves the config readable and reports the validation error. A catalog edit invalidates any configuration review already open in a browser.

When a new built-in preset matches an existing vendor overlay and parameters, the menu keeps the vendor entry's ID and label and lists it once. If the vendor already uses the preset's ID for a different configuration, the vendor keeps its ID and the built-in receives an alternate ID. API clients should use the IDs returned by `GET /api/firmware`.

A matching vendor overlay already in the global section can be adopted, including its listed parameters in a different order. Keep the profile in the catalog while its managed overlay is in use. To exercise this locally, put the JSON file under `.dev-state/firmware-sim/sensor-profiles.json` and create a matching empty fixture at `.dev-state/firmware-sim/boot/overlays/vendor-module.dtbo` after starting the simulator. On a board use the vendor's compiled overlay. [Raspberry Pi firmware parameters](https://www.raspberrypi.com/documentation/computers/config_txt.html)

## Existing boot files and recovery

The editor can adopt recognized global camera overlay directives for one connector and global `camera_auto_detect` assignments. When several plain global directives target that connector, the operator selects the one to stage. The editor preserves unrelated lines, line endings, and board-specific settings. It writes the managed block at the end under an explicit `[all]` section, so the final scalar `camera_auto_detect` applies after any earlier included or conditional assignments. Overlays accumulate in the firmware. Resolve the conflict shown in the panel before staging if definitions span multiple connectors, combine a managed block with an active unmanaged overlay, occur in conditional sections or included files, use unrecognized parameters or depend on a custom firmware prefix. Existing include files retain their original contents. The read-only view shows the top-level file on disk. Firmware EEPROM additions and external edits after boot can also affect the running device. [Raspberry Pi config.txt reference](https://www.raspberrypi.com/documentation/computers/config_txt.html)

The helper verifies the board and the Raspberry Pi OS boot partition before opening the configuration. If the panel asks for the boot partition, inspect `findmnt /boot/firmware` on the target. The editor requires the firmware volume to be mounted separately at that path. This prevents saving profiles to a stale directory on the root filesystem when the drive is absent.

Before staging a change, the helper saves the existing file as `/boot/firmware/config.txt.rpi-web-control.bak`. On a separate computer, this file is next to `config.txt` on the boot volume. If the board cannot boot, restore the backup in its place on that volume and safely eject the drive. Undo through the UI uses the saved content and SHA-256 revision in the root-owned `/var/lib/rpi-web-control-firmware/` directory. A revision mismatch requires refreshing the panel. The service compares Linux boot IDs to show whether its latest write still awaits a reboot; the running sensor details come from libcamera.

## API and simulation

| Operation | Request |
| --- | --- |
| Inspect config and profiles | `GET /api/firmware` |
| Stage IMX708 on CAM0 | `POST /api/firmware` with `{"profile":"imx708","port":"cam0","revision":"<revision>"}` |
| Stage OV9281 on CAM0 | `POST /api/firmware` with `{"profile":"ov9281","port":"cam0","revision":"<revision>"}` |
| Stage the four-lane IMX577 on CAM0 | `POST /api/firmware` with `{"profile":"imx577","port":"cam0","revision":"<revision>"}` |
| Restore the previous file | `POST /api/firmware` with `{"profile":"restore","port":"cam1","revision":"<revision>"}` |
| Confirm reboot | `POST /api/reboot` with `{"confirm":true,"revision":"<revision>"}` |

Read the profile ID from the catalog when using a vendor profile. Each profile's `directives.cam0` and `directives.cam1` show the configuration it will stage for that connector. Use the review revision returned by the last read or write. It also covers the optional vendor catalog. Firmware and reboot requests follow the console's token setting. The port field has no effect for auto and restore. The privileged socket helper restricts write requests to validated sensor profiles and a fixed reboot command. The web process runs under its own unprivileged account.

In simulator mode, edits and boot IDs live under the simulator state directory. The reboot action advances the simulated boot ID so pending-state and recovery workflows can run on a development computer. Real CSI enumeration, startup and physical reboot are covered by the hardware acceptance run.
