# Hardware acceptance

Use this procedure on a test Raspberry Pi 5 and a Compute Module 5 with its intended carrier. Record each result against the exact board, sensor, lens, carrier, storage device and OS image. Mark untested combinations as pending.

Previous Raspberry Pi 5 bench results include [IMX577 capture rates](pi5-imx577-results.md) and [OV9281 preview at several sensor settings](hardware-results/pi5-ov9281-preview.md).

## First SSH connection and inventory

Connect the Pi and workstation to the same LAN. Use the login and hostname configured for this board. The examples use `operator@capture-pi.local`. If local name resolution is unavailable, use the address shown on the Pi by `hostname -I` or in the router's DHCP lease. On a fresh image, enable SSH in Raspberry Pi Imager and configure the operator's public key. On an existing image, SSH can be enabled from the Pi with `sudo raspi-config`. [Raspberry Pi remote access](https://www.raspberrypi.com/documentation/computers/remote-access.html)

On first connection compare the SSH host-key fingerprint with the same key on the Pi's local console. For an ED25519 host key use `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the Pi. From the project root on the workstation, collect the read-only report before installing the package. The redirection saves it locally. Use a fresh filename for each board and run.

```sh
ssh operator@capture-pi.local 'sh -s' < tools/inspect-board.sh > "capture-pi-$(date -u +%Y%m%dT%H%M%SZ).txt"
```

Check the reported model and arm64 package architecture, OS release, boot volume and intended recording destination. The report enumerates camera modes before installation or while the controller is inactive. With the service running, use the **Camera** page for the detected sensor and its modes. Keep the pre-install report with the later capture results.

## Transfer, install and open the console

From the workstation, copy the verified archive to the operator's home directory and open an SSH shell on the target.

```sh
scp dist/rpi-web-control_0.9.0-2_arm64.deb operator@capture-pi.local:
ssh operator@capture-pi.local
```

On the Pi, run the following commands from the operator's home directory. When upgrading an existing installation, stop active recordings first.

```sh
sudo apt install ./rpi-web-control_0.9.0-2_arm64.deb
systemctl is-active rpi-web-control.service
sudo openssl x509 -in /etc/rpi-web-control/tls.crt -noout -fingerprint -sha256
```

Open `https://capture-pi.local:8080` using the actual hostname. Compare the browser's certificate fingerprint with the TLS fingerprint displayed through SSH. If token protection is enabled, retrieve the device token with `sudo cat /etc/rpi-web-control/token` in the trusted shell and enter it in the console. Keep this credential separate from the hardware report. See the [package guide](package.md) for custom certificates and service diagnostics.

If the console reports a clock difference during offline commissioning, compare `date -u` on the Pi with the browser device's UTC time. With recording stopped and all trigger claims released, select **Set Pi time**. Check the Pi's UTC time again, then refresh the console and verify the warning has cleared.

## Measure every advertised sensor mode

### From the console

1. Finish any recording, release its trigger claims and turn off browser preview and live streaming. Open **Camera → Test sensor modes**. Under **Mode to test**, choose one resolution and bit depth, or leave **All detected modes** selected.
2. Choose **Run test**. Each mode runs at its advertised maximum with a half-second warmup and a one-second measurement from the camera's sensor timestamps. Check the measured FPS and result for each entry, including entries that have the same resolution at different bit depths. A failed row includes the reason. **Cancel test** releases the camera. A new manual or trigger recording request also ends the test so recording can begin.
3. Results remain available in the console until the next test or service restart. Capture testing runs in memory and preserves saved camera settings. To check recording or network throughput, continue with [capture and playback](#capture-and-playback) or [live reception](live.md).

### From a workstation over SSH

Use the SSH account that can access the CSI camera and the system Python from the installed controller package (0.2.0-2 or later). Begin with the camera idle. Finish active recordings, turn off preview and live streaming, and choose a test window when a recording trigger will not fire. The checker opens the camera exclusively and releases it between modes. An existing camera owner will cause acquisition to fail. The checker leaves the service configuration, storage and firmware alone.

Run this command from the workstation checkout. It sends the checker over SSH, prints progress in the terminal and saves the JSON report on the workstation. Use a fresh output name for each run.

```sh
ssh -T ubuntu@192.168.55.100 '/usr/bin/python3 -' < tools/check-camera-modes.py > "camera-modes-$(date -u +%Y%m%dT%H%M%SZ).json"
```

The checker runs the installed capture adapter with OSD off and a YUV420 main stream. It requests the advertised maximum for every sensor size and bit depth. For each qualifying sensor mode it also requests 1280 × 720 output at 240 fps, skipping an identical native test. The supplied IMX577 inventory advertises four native resolutions at each of 8 and 10 bits. Adding 720p240 at both depths gives ten tests. This count is a test plan derived from the inventory.

Each run discards one second of warmup and measures two seconds using the completed requests' integer `SensorTimestamp` values in BOOTTIME nanoseconds. The JSON includes the sensor identity, board, boot ID, requested frame duration in microseconds, negotiated dimensions / format / depth, frame count, observed rate, maximum interval, gap count and queue drops. The measured rate is the number of intervals divided by the elapsed sensor time. A pass requires matching negotiation, a complete sample within 5% of the target, no capture queue drops and no interval longer than 1.5 requested periods. The estimated missing periods describe timing gaps. Capture, encoder, network and display throughput can differ.

To repeat only the 1920 × 1080, 8-bit sensor mode with a longer sample, run:

```sh
ssh -T ubuntu@192.168.55.100 '/usr/bin/python3 - --mode 1920x1080:8 --warmup 2 --seconds 30' < tools/check-camera-modes.py > imx577-1080p8-retest.json 2> imx577-1080p8-retest.log
```

`--mode WIDTHxHEIGHT:BITS` selects a native sensor entry and runs its planned frame rates. Omitting it exercises the full inventory. Preserve the initial matrix and the follow-up as separate samples. Discovery has a 30-second deadline. Each case has its own deadline of warmup plus sample length plus 20 seconds, including setup and cleanup. Exit 0 means all selected cases passed, 1 means at least one case failed, 2 means discovery, mode selection or arguments failed, and 130 means the matrix was interrupted. Continue with the destination-specific recording and receiver checks below to measure the full data path.

### If discovery reports a busy camera

If the terminal reports `Pipeline handler in use by another process` and the JSON contains `discovery_failed` with empty results, the camera was detected by libcamera and the checker could not acquire it. No mode has been measured. Use the CFE and ISP device paths from that run's registration message. For the IMX577 board that reported `/dev/media3` and `/dev/media0`, run this on the Pi while the problem is present:

```sh
systemctl show --no-pager -p MainPID rpi-web-control.service
sudo lslocks --notruncate --noheadings --output PID,COMMAND,PATH | grep -E '/dev/media(0|3)$'
sudo fuser -v /dev/media3 /dev/media0
```

Compare the service PID with any reported media lock. `fuser` lists processes with open devices. PipeWire and WirePlumber can appear there during desktop camera discovery. An open device alone does not identify which process held the capture lock at the time of failure. [Debian fuser manual](https://manpages.debian.org/trixie/psmisc/fuser.1.en.html) · [libcamera pipeline acquisition](https://docs.libcamera.org/master/internal-api/classlibcamera_1_1PipelineHandler.html)

If the controller is using the camera, select **Disable preview** in **Monitor**, select **Stop live stream** in **Stream** if active, and finish any manual or trigger-owned recording. Closing the browser leaves these controls in their existing state. If a desktop application is actively using the camera, end capture in that application. Retry with a fresh report filename. When only desktop device monitors are listed and no lock is visible, retry before changing any services.

## Capture and playback

Save the test board's advertised sensor modes. A 240 fps acceptance run requires a mode whose listed maximum supports that rate at the requested dimensions. Log cooling, thermal throttling, input power, and free space on the recording volume. At 640 × 480 and 240 fps, 8-bit YUV420 alone produces 110.592 MB/s (105.47 MiB/s) or about 6.64 GB per minute, plus frame headers and metadata. Budget free space before a sustained test. For a wireless CM5, record the radio and antenna setup. For a CM5 without Wi-Fi, also check operation over Ethernet.

Start with a local clip at 30 fps. After it passes, use the [live receiver guide](live.md) to repeat the test on the receiving PC, then test the high-speed mode.

1. Select a supported resolution at 30 fps. Start recording with preview off. Enable preview, open a second viewer, close both viewers, then disable preview. The recording must cover the full interval.
2. Make a second capture with OSD. Verify the text in recorded frames during playback. Record the effective OSD state at the selected rate.
3. Repeat with the highest required sensor mode and OSD disabled. Capture at least 60 seconds on the intended destination. Repeat with preview enabled and a slow consumer. Record the requested and measured FPS, number of stored frames, largest frame interval, any dropped-frame indicators, and visible changes to the preview. Include the dimensions when reporting a 240 fps result.
4. Stop recording cleanly. Verify that the sidecar entry count equals the number of complete video frames and that sensor times advance. Inspect a frame near the beginning, middle, and end of the clip.
5. Repeat the selected test after enough sustained recording to reach thermal equilibrium and fill the storage device's write cache. Observe CPU temperature, throttling, and free space during this interval. A short run measures initial throughput.

Run the checker from the project checkout against the completed session directory. Set `--fps` to the requested rate for that recording.

```sh
python3 skills/rpi-capture-development/scripts/check_recording.py /path/to/session --fps 240
```

The checker walks `frames.csv` and the stored video together. For `video.y4m` it checks the frame headers and planar payload sizes. For `video.mjpeg` it checks JPEG boundaries. It also checks consecutive frame indices, complete byte coverage, increasing sensor timestamps, and the largest frame interval. Decode the clip separately to check pixel content. For example, run `ffmpeg -v error -i video.y4m -f null -` from the recording directory. Substitute `video.mjpeg` for a normal-rate recording. Inspect the timing counts even when the format checks pass.

Use positive sensor-time differences to calculate the median frame interval. Estimate achieved FPS as `(frames - 1) * 1e9 / (last_sensor_ns - first_sensor_ns)`. Flag each interval above 1.5 times the nominal frame period for inspection. Quantify missing frames separately if the capture format carries sequence information. For a strict lossless 240 fps claim, require one indexed frame for every stored frame, zero unexplained gaps, and report the measured rate with its precision.

## Correlate frames and logs

At the beginning and end of a recording, write a journal marker using `logger -t rpi-capture-check "capture marker"`. Save the following along with the recording.

```sh
cat /proc/sys/kernel/random/boot_id
journalctl -b -t rpi-capture-check -o json > timing-markers.jsonl
journalctl -b -u rpi-web-control -o json > controller-journal.jsonl
```

For each marker use `_BOOT_ID`, `__MONOTONIC_TIMESTAMP`, and `__REALTIME_TIMESTAMP`. Compare boot IDs after stripping hyphens and convert journal microseconds into nanoseconds before matching. Obtain the boot ID from `session.json`. Compare a marker against the closest recorded `monotonic_ns` for journal monotonic time, or `realtime_ns` for journal wall-clock time. Retain the original `sensor_ns` alongside both derived estimates. Journal receipt, camera exposure and application callbacks are distinct events. Record the observed separation and the per-frame clock sampling window in `clock_sample_ns`. For an independent check of exposure-to-input latency, capture a visible LED transition that is also timestamped by the test fixture. Compare the first changed frame and fixture event using the same time domain.

The sensor timestamp refers to the first active row and uses Linux BOOTTIME. Journal monotonic timestamps use MONOTONIC. These clocks can diverge after suspend. Re-check the clock mapping across wall-clock adjustments or suspend before using the recording for absolute timing. [libcamera controls](https://github.com/raspberrypi/libcamera/blob/main/src/libcamera/control_ids_core.yaml) · [Journal fields](https://www.freedesktop.org/software/systemd/man/latest/systemd.journal-fields.html)

## Recording triggers

| Source | Bench action | Evidence |
| --- | --- | --- |
| Timer | Use a short interval and duration. Observe two cycles. | Each cycle starts and ends a recording claim. |
| GPIO NO/NC | Wire a dry contact to the selected BCM pin and ground. Test normally open, then normally closed behavior on the actual carrier. Exercise contact bounce. | Idle input, asserted input and release match the selected polarity and debounce. |
| GPIO with separate stop | Follow the [contact wiring examples](triggers.md#gpio). Start on GPIO17 and stop first with GPIO27, then with a serial line. Repeat with the start held open and with manual recording active. | At initial arm an open start contact stays idle. Each new start opens one clip. The selected stop releases only its own rule, and a held start requires a new contact cycle. |
| Sensor | Select a readable supported sysfs sensor. Move its reading across the threshold and recovery boundary. | Log raw readings and units, both transitions, and behavior inside hysteresis. |
| UART | Use the configured serial interface and baud rate. Send the configured start and stop lines. | Both commands change the rule state. An unrelated line leaves recording ownership unchanged. |
| Overlap | Start manually, assert a rule, release manual control, then release the rule. Repeat with two rules. | One uninterrupted clip covers the interval with at least one active source. |

Exercise a configured input that is absent or unavailable. Check the error against the rule ID and confirm that unrelated functions remain usable.

## Storage, service and networking

- On a spare PCIe NVMe disk with an existing ext4 partition, place a test file on the partition and retain its SHA-256. Unmount it and choose **Mount existing volume** under **PCIe NVMe setup**. Verify the file and checksum after mounting. Select **Record to NVMe**, verify the active directory and record a clip. After a normal reboot, confirm the same filesystem UUID with `findmnt --mountpoint /srv/rpi-web-control-nvme -o SOURCE,UUID,FSTYPE,OPTIONS` and record another clip.
- With explicit permission to erase a **disposable** PCIe SSD, verify its model and serial, then use **Erase disk and create ext4 volume**. Verify the resulting GPT, ext4 UUID and mount using `lsblk -o NAME,MODEL,SERIAL,PTTYPE,FSTYPE,UUID,MOUNTPOINTS`. Record at the intended mode and duration. Keep the measured rate, dropped frames and SSD identity with the acceptance results.
- Repeat the mount and recording checks with a spare USB flash drive. Select **Record to USB**, record a clip, then confirm the destination is on `/srv/rpi-web-control-usb`. Use a separate disposable device for any USB initialization test.
- Check the cleanup controls with completed test sessions on the managed drive, another directory containing an unrelated file, and a completed clip on the system card. Review a single-session deletion, then a whole-directory cleanup. Check counts against the eligible sessions. On the test volume, confirm that the selected complete sessions are removed and unrelated data remains. Disk preparation and recording deletion must exclude every partition on the system disk. Replace the test volume between review and confirmation and verify that the stale request is refused.
- Switch the destination back to the system card. Browse the previous clip on the managed NVMe SSD and select **Download recording (.zip)**. Compare the SHA-256 of each ZIP member (video, `frames.csv` and `session.json`) with the original on the SSD, then run the recording checker against the unpacked session.
- Choose a writable recording directory on the intended volume. Verify the actual output location and repeat across a service restart. Try an unauthorized path and an unwritable destination. The service must show the error and leave previous recordings readable.
- Check storage exhaustion with a small disposable test volume. Record the last readable frame, sidecar consistency, reported fault, and recovery after selecting a writable destination.
- With the package defaults, open the console in a fresh browser session and start and stop a recording without entering a token. Enable token sign-in, restart the service and check that a request to `/api/status` without credentials returns HTTP 401. Sign in and repeat the recording check. After upgrading, verify the configured access mode, credentials, earlier recordings and clean service shutdown.
- For each board and adapter, record interface names, driver, NetworkManager version, client addressing and observed connectivity. From Ethernet, trial a fixed IP, reconnect to the new address, confirm and repeat the connection after reboot. Trial DHCP and let it expire, then check that the previous IP works again. On a wired-only CM5, exercise Ethernet with the radio absent.
- Connect a workstation directly at `192.168.55.10/24` with no DHCP server. After boot and again after reconnecting the cable, open `https://192.168.55.100:8080`. Check the **Wired recovery** state, including **Already assigned** when the primary address is also `.100`. Repeat with the cable in the managed bridge and verify that the main address, gateway and DNS remain usable. [Wired recovery procedure](network.md#connect-by-ethernet-when-the-main-address-is-unknown)
- From Ethernet with the software Wi-Fi radio off, open **Network → Wi-Fi access point**, enable Wi-Fi and start a standalone AP. Join from a phone, check its DHCP lease, open the displayed URL over Wi-Fi and confirm the trial. With a client attached, request a scan on the AP radio. Cancel the warning and verify that no scan starts. Confirm a second request and record whether the driver returns neighboring networks. Verify the AP profile, client DHCP and Ethernet access afterwards. Check the saved SSID after reboot. Repeat with automatic rollback and verify the previous profile and wired console are usable. On a wired-only board, check the message for a missing radio. [AP setup](network.md#create-a-wi-fi-access-point)
- With Ethernet at a known static address, scan from a Wi-Fi adapter, select an SSID, enter its password and connect. Check the DHCP lease and open the console from the wireless LAN. Verify the wired IP and the separate Wi-Fi IP during the trial, after confirmation and after reboot. Repeat with an incorrect password, a hidden SSID and an expired trial. [Join Wi-Fi](network.md#join-an-existing-wi-fi-network)
- For a bridge, verify `br-rpi` owns the Pi's address and both selected links appear in `ip link show master br-rpi`. Connect one client by cable and one by Wi-Fi. Verify HTTPS access from both, client-to-client communication and the source of their DHCP leases. Test upstream DHCP on an existing LAN and Pi-provided DHCP on an isolated LAN. Let an unconfirmed bridge change expire and check both ports return to their previous state. [Network setup](network.md)

Keep results as an acceptance worksheet with columns for board/carrier, sensor mode, destination, requested rate, measured rate, maximum interval, unexplained gaps, OSD/preview state, duration and pass/fail. Retain the clip, sidecar, saved journal entries and package version for each reported throughput result.

### Wi-Fi inventory before the access-point update

On 19 November 2026 the connected Raspberry Pi 5 Model B Rev 1.1 reported a managed `wlan0` with AP capability. NetworkManager reported hardware wireless enabled and software wireless disabled. The configured WLAN country was `UA`. NetworkManager 1.52.1, `python3-dbus` and `dnsmasq-base` were installed. The application's AP profile serialized with the expected D-Bus signature on the Pi. NetworkManager's offline check also accepted an equivalent WPA-protected profile with shared IPv4 at `10.42.0.1/24`. The live Ethernet stream continued during these checks.

The operator subsequently reported that a client joined the Pi's AP and received a DHCP lease. The station picker returned no networks on their setup. Repeating the station scan on physical hardware and recording the adapter mode, NetworkManager's cached AP count and the picker result is still pending. [Empty scan checks](network.md#if-the-list-of-nearby-networks-is-empty)

## Validate a new sensor, volume and remote stream

1. Record the detected sensor model and modes, then stage a supported boot profile on the correct CSI connector. Compare the configuration before and after staging. Verify that an active recording blocks a reboot. After a reboot, reopen the console and confirm the real sensor model and mode. Power down before moving a CSI cable. Exercise the documented profile restore.
2. On every physically present medium, check the reported transport, mount, writable state and free capacity. Identify which device backs the root filesystem. Use a mounted data filesystem for an external SPI flash test. A raw MTD device should have no selectable recording destination. With recording stopped, unmount a removable volume, request a new recording, and check that the underlying system disk receives no session.
3. On each available link, test the live receiver at a supported 240 fps geometry with an empty **Stream FPS limit**, then repeat with a limit of 60. Check preview off/on and simultaneous recording. Compare sensor-timestamp cadence in the recording with the sender rate and independently decoded receiver rate. For each interval, record the changes in **Queue replacements**, **UDP retries**, **Network congestion drops** and **Skipped by FPS limit**. Record receiver refresh rate, loss, and median and tail glass-to-glass latency as described in [live reception](live.md).
4. Download a complete high-speed session ZIP and transfer a timestamped telemetry log. Render the ZIP on the workstation. Use ffprobe to compare the frame count and presentation times with the frame index, including any capture gap. Export a second MP4 with `--timing constant --fps 240` and verify the constant rate. Inspect OSD on a known channel transition, a pre-sample frame and a stale interval. Keep the original CSV and video for timing analysis.

### CM5 live observations before the sender update

On 19 November 2026 a Compute Module 5 Lite streamed 640 × 480 to a workstation from a 1280 × 720, 8-bit sensor mode with capture set to 240 fps. Recording and browser preview were off. Two status readings about one second apart showed 240.14 fps capture, zero estimated missed capture frames and zero camera queue drops. Sender throughput was about 79 Mbit/s and its rate was about 240 fps. Its combined drop counter remained at 8,376 across the two readings. The earlier sender reported a single total for queue replacements and send failures, so the source of those accumulated drops is unknown. Receiver FPS and display latency were unmeasured. An on-board repeat with the updated sender and a 60 fps stream limit is pending.
