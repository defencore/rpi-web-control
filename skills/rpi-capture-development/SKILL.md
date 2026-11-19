---
name: rpi-capture-development
description: Use when changing Pi 5 or CM5 camera discovery, capture modes, preview, on-screen text, recording persistence, RTP/JPEG streaming, telemetry rendering, or per-frame time correlation in rpi-web-control.
---

# Camera development

Inspect `rpi_web_control/capture.py` for completed requests and sensor selection, and `rpi_web_control/camera.py` for acquisition lifetime. The frame index and session manifest live in `rpi_web_control/recording.py`. Trace user-visible changes through the controller, API, and browser before updating the docs.

## Change a capture mode

1. Match the camera's reported geometry, maximum FPS and sensor bit depth. Preserve automatic depth selection (`sensor_bit_depth: 0`) for old configurations. With an explicit depth, test both entries when a sensor lists the same geometry twice. On Pi 5, pass the selected size and depth in Picamera2's `sensor` dictionary and check the negotiated values before capture. Verify that the active camera status and session manifest agree with the selected mode. [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
2. Budget capture, preview, and storage separately. At 240 fps a frame arrives about every 4.17 ms. Keep network clients and rendering off the acquisition path. Preserve bounded queues and report any overrun or stall.
3. Specify the recorded representation, dimensions, stride, and timestamp schema together. An aligned pixel buffer may be larger than the requested image dimensions. Check an exported session with `python3 skills/rpi-capture-development/scripts/check_recording.py SESSION --fps RATE` from the repository root. Follow the offline procedure in [hardware validation](../../docs/hardware-validation.md).
4. Exercise recording with preview off, one preview client, then a slow client. Toggle preview while recording. Check the frame index and sensor times after each change. Test the OSD policy at its rate boundary.
5. Exercise failure while opening the camera, selecting a mode and writing a frame. A worker failure must be observable, and a later acquisition must have a defined recovery path.
6. Use the [mode checker](../../tools/check-camera-modes.py) on the connected Pi to measure sensor cadence at each advertised size and depth, including 240 fps where supported. Follow [hardware validation](../../docs/hardware-validation.md) for the separate recording and receiving measurements.
7. Treat discovery failure with an empty result list as an unmeasured run. Correlate the CFE and ISP media locks with the controller PID before attributing a busy camera to a service. Compare timestamps when the diagnostic snapshot follows the failed attempt.
8. Exercise the console test with one advertised mode, the whole inventory and a cancellation. Keep equal-size entries at different bit depths distinct. Check sample FPS against completed requests' sensor timestamps, surface a failed mode, and verify capture works again with its saved settings. Cover preview, streaming and recording ownership around a test.

## Change timing or metadata

- Use the sensor timestamp from the same completed camera request as the stored pixels. Preserve integer nanoseconds. Track write completion, dropped frames, and missing metadata separately.
- Associate a recording with its boot identity and clock conversion data. Journal timestamps use microseconds. A wall-clock adjustment can affect UTC estimates, and suspend can change the difference between BOOTTIME and MONOTONIC.
- Compare the video frame count, number of index entries, and actual camera cadence. An FPS setting is a request. A measured FPS claim needs the board, sensor mode, storage path, run duration, timestamps, and observed gaps.
- Camera status reports the current video file's logical length in `recording_video_bytes`, including Y4M headers. Once at least one frame has been recorded and one monotonic second has elapsed, `recording_video_bytes_per_second` divides that length by elapsed seconds since clip start. Compare a live snapshot with the indexed end offset of its last frame in both formats. Idle status reports zero bytes and a null rate.

[libcamera timing controls](https://github.com/raspberrypi/libcamera/blob/main/src/libcamera/control_ids_core.yaml) · [Journal timestamp fields](https://www.freedesktop.org/software/systemd/man/latest/systemd.journal-fields.html)

## Change the live stream or desktop exporter

- Keep the live sender, browser preview and recorder independently controllable. Apply any stream FPS limit using sensor timestamps before retaining or queuing the frame. Check 240 fps capture with a 60 fps stream and distinguish intentional skips from sender drops.
- Release the sender's retained camera request as soon as JPEG encoding finishes. Simulate a busy socket mid-JPEG and verify that a retry sends the same fragment. With sustained pressure, check bounded waiting, camera request release, queue replacements, congestion drops and recovery. Keep other encoding and network errors visible.
- When changing the receiver endpoint, send from the simulator to a UDP listener with an address different from the HTTP peer and decode a frame. For link-local IPv6, check the interface index in the socket destination.
- For any RTP change, verify baseline JPEG quantization tables, fragmentation offsets, marker bits, sequence wrap and sensor-derived 90 kHz timestamps. Decode emitted packets with a receiver independent of the sender. Board measurements need camera, sender and receiver rates plus glass-to-glass latency. See [live reception](../../docs/live.md).
- When changing the frame index or session manifest, run the desktop exporter tests against both MJPEG and Y4M and check the final H.264 frame count, rate and rendered overlay. Keep nanosecond clocks as integers, verify telemetry boot IDs where present and exercise missing/stale samples and cadence gaps. See [telemetry rendering](../../docs/post-processing.md).
- Test the downloadable converter with a session ZIP and a directory. For a clip with a capture gap, use ffprobe to compare sensor-timed presentation timestamps with the source frame index and verify the constant-rate export independently.
