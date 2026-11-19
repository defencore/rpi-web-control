# OV9281 preview on Raspberry Pi 5

Tested on a Raspberry Pi 5 with controller 0.3.0-1 and libcamera v0.7.2+rpt20260817. The boot configuration selected `dtoverlay=ov9281,cam0`. The camera identified itself as a monochrome OV9281. All native checks requested three YUV420 frames in memory with `rpicam-vid`. These counts establish frame delivery at the requested settings. They do not measure sustained FPS.

| Output | Sensor bits | Requested FPS | Complete frames | Observation |
| --- | ---: | ---: | ---: | --- |
| 640 × 400 | 10 | 247.83 | 1/3 | The camera frontend timed out. |
| 640 × 400 | 10 | 30 | 3/3 | Completed. |
| 1280 × 800 | 10 | 30 | 3/3 | Completed. |
| 1280 × 800 | 8 | 30 | 3/3 | Completed. |
| 640 × 400 | 8 | 30 | 3/3 | Completed. |
| 640 × 400 | 8 | 240 | 3/3 | Completed. |

At the advertised maximum of 247.83 fps in 10-bit mode, the controller negotiated the requested mode and started capture. The camera frontend timed out before the controller could deliver a preview JPEG. The native test independently reproduced the timeout. The sensor and the CSI path delivered frames at the other settings above with the same boot configuration.

In **Camera settings → Video settings**, select the matching 640 × 400 entry, enter the desired **Target frame rate (fps)** and choose **Save video settings**. In **Capture monitor**, choose **Start preview**. The browser samples the capture at up to eight JPEG frames per second. **Measured frame rate** under **Camera activity** refers to the sensor.
