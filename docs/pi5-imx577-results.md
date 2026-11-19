# IMX577 capture on Raspberry Pi 5 Model B Rev 1.1

On 18 November 2026 at 15:45 UTC, the controller's Pi camera adapter acquired all ten requested configurations. Nine passed the capture-only acceptance criteria. Both 720p240 samples delivered 482 frames with increasing sensor timestamps and zero gaps.

| Output | Target FPS | Sensor bits | Measured FPS | Frames | Longest interval (ms) | Gaps | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1280 × 720 | 240 | 8 | 240.136 | 482 | 4.165 | 0 | Pass |
| 1280 × 720 | 240 | 10 | 240.135 | 482 | 4.165 | 0 | Pass |
| 1280 × 720 | 364.70 | 8 | 364.581 | 731 | 2.751 | 0 | Pass |
| 1280 × 720 | 364.70 | 10 | 364.581 | 731 | 2.745 | 0 | Pass |
| 1920 × 1080 | 175.47 | 8 | 174.966 | 351 | 11.395 | 1 | Fail |
| 1920 × 1080 | 175.47 | 10 | 175.466 | 352 | 5.700 | 0 | Pass |
| 4056 × 2160 | 42.43 | 8 | 42.440 | 86 | 23.564 | 0 | Pass |
| 4056 × 2160 | 42.43 | 10 | 42.440 | 86 | 23.565 | 0 | Pass |
| 4056 × 3040 | 30.34 | 8 | 30.346 | 62 | 32.955 | 0 | Pass |
| 4056 × 3040 | 30.34 | 10 | 30.346 | 62 | 32.955 | 0 | Pass |

At 1080p with 8-bit sensor readout, one 11.395 ms interval spanned approximately two nominal frame periods. The mean rate was within the 5% tolerance. The gap exceeded the limit of 1.5 frame periods. The adapter reported zero queue drops throughout the matrix. These counters do not identify where the gap originated.

## 30-second repeat at 1920 × 1080, 8-bit

At 15:48 UTC on the same board, boot and controller version, a targeted run requested 175.47 fps and allowed two seconds of warmup. It delivered **5,265 frames at 175.466 fps** over 30.000187 seconds of sensor time. The longest interval was 5.705 ms. There were zero timestamp gaps, non-increasing intervals or adapter queue drops. The repeat passed. The original short sample above retains its single observed gap.

## Test conditions

- Raspberry Pi 5 Model B Rev 1.1, IMX577, kernel `6.18.50+rpt-rpi-2712`, controller `0.3.0-1`, Picamera2 `0.3.37-1`, Python libcamera package `0.7.2+rpt20260817-1`.
- The system negotiated the exact sensor size and bit depth plus the requested YUV420 main output in every case. OSD was off. Each sample used one second of warmup and at least two seconds of measurement using libcamera's BOOTTIME `SensorTimestamp`. FPS uses the number of intervals divided by the elapsed sensor time.
- Frames were consumed in memory. Storage throughput, RTP/JPEG decoding on a remote computer, display latency and sustained capture require separate measurements. The [hardware procedure](hardware-validation.md) covers those tests.
- The initial attempt at 15:41 UTC on controller `0.2.0-2` failed to acquire the pipeline before collecting any frames. A later device snapshot showed PipeWire and WirePlumber with open media nodes. The original lock holder was not established.
