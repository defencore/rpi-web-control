# Receive live video on a remote computer

The **Stream** page sends color RTP/JPEG over UDP. By default the sender attempts to transmit every captured frame. You can limit the stream rate while capturing and recording at the selected camera rate. The browser's **Monitor** page offers a separate MJPEG preview.

## Install GStreamer on the receiving computer

### Windows

1. Open the GStreamer download page and select **Windows → MSVC x86_64** for an Intel or AMD PC, or **MSVC arm64** for a Windows on Arm PC. Run the installer. In the 1.28 and later installer choose **Only runtime**. [Official Windows downloads](https://gstreamer.freedesktop.org/download/)
2. Open a new Command Prompt and run `gst-launch-1.0 --version`. If Windows cannot find the command, find `gst-launch-1.0.exe` in the installed runtime and add its containing `bin` directory to your **user Path** under **Edit environment variables for your account**. For a default per-user x64 installation this is `%LOCALAPPDATA%\Programs\gstreamer\1.0\msvc_x86_64\bin`. A system-wide x64 installation uses `%ProgramFiles%\gstreamer\1.0\msvc_x86_64\bin`. For Arm64 or a custom install, use the location you selected in the installer.
3. Close and reopen your terminal after changing Path. In PowerShell you can check the selected executable with `Get-Command gst-launch-1.0`. Use a Command Prompt tab for the playback commands below.

### Linux

On a Debian or Ubuntu desktop, install the tools, plug-ins and display sinks with APT.

```sh
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-gl gstreamer1.0-x
```

On a Fedora desktop, install the corresponding packages with DNF.

```sh
sudo dnf install gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good
```

Run the receiver from a terminal in your graphical desktop session. [GStreamer Linux packages](https://gstreamer.freedesktop.org/documentation/installing/on-linux.html)

### macOS

If you use Homebrew, run the following command in Terminal. [Homebrew GStreamer formula](https://formulae.brew.sh/formula/gstreamer)

```sh
brew install gstreamer
```

If you use the official installer, choose **macOS Universal → runtime installer** and open the downloaded `.pkg`. It supports both Apple silicon and Intel. In a new Terminal window, run the following to use the framework's tools for this session. [Official macOS downloads](https://gstreamer.freedesktop.org/download/) · [GStreamer framework path](https://gstreamer.freedesktop.org/documentation/installing/on-mac-osx.html)

```sh
export PATH="/Library/Frameworks/GStreamer.framework/Versions/1.0/bin:$PATH"
```

### Check the receiver before connecting

Run these commands on the receiving desktop. Each inspection should print details for the named element. The last command opens a window with a test pattern for about three seconds and then exits.

```text
gst-launch-1.0 --version
gst-inspect-1.0 udpsrc
gst-inspect-1.0 rtpjpegdepay
gst-inspect-1.0 queue
gst-inspect-1.0 jpegdec
gst-inspect-1.0 autovideosink
gst-launch-1.0 videotestsrc num-buffers=90 ! videoconvert ! autovideosink
```

If an inspection reports a missing element, check the install above. The UDP source, RTP/JPEG depayloader, JPEG decoder and automatic video sink are part of the **Good** plug-ins. If inspection succeeds but no test pattern appears, check that the receiver has an active graphical session and a working video sink.

## Connect and start playback

1. Connect the receiving computer and Pi via Ethernet or Wi-Fi. In station mode use the Pi's LAN address. In AP mode join the Pi's Wi-Fi network and use its AP address. Check a connected Pi with `hostname -I`, or use its DHCP lease. A routed VPN can carry HTTPS and UDP between sites.
2. Open `https://<pi-hostname>.local:8080`. Verify the server certificate on first use. If the console prompts for a token, read it via trusted SSH with `sudo cat /etc/rpi-web-control/token` and sign in. [Package guide](package.md)
3. In **Camera**, select the resolution and frame rate. For a 240 fps run, select a sensor mode reporting at least 240 fps at the required dimensions. The IMX577 on the inventoried Pi 5 reports 364.70 fps at 1280 × 720 and 175.47 fps at 1920 × 1080. Select 1280 × 720 for a 240 fps trial on that board. RTP/JPEG encodes each output dimension in units of eight pixels, with a maximum of 2040 pixels per dimension. Other sizes remain available for local capture. Stop a running stream before changing the camera mode.
4. Allow inbound UDP on the receiving computer from the Pi's address on the selected port (default 5004). On Windows, select the firewall profile used by this network. On macOS, allow incoming connections for the GStreamer receiver if prompted.
5. Open **Stream**. Leave **Receiver IP** empty to use the address of the device that starts the stream. To control the Pi from a phone and watch on a MacBook, enter the MacBook's Ethernet or Wi-Fi address, for example `192.168.55.10` if that is its address on the Pi's LAN. Also enter the receiver's address when the console is behind a reverse proxy or SSH port forward. The Pi needs a route to the address you enter. Set **Receiver UDP port** and select **Copy receiver command**. Run the command in Terminal on the receiving Mac or Linux computer, or in Command Prompt on Windows. Leave that terminal open while receiving. This example listens on IPv4 port 5004.

```sh
gst-launch-1.0 -v udpsrc port=5004 buffer-size=1048576 caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload=26" ! rtpjpegdepay ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! jpegdec ! autovideosink sync=false
```

6. Leave **Stream FPS limit** empty to send at the camera rate, or enter an upper limit. For example, with the camera set to 240 fps, enter `60` to send about one frame in four and record at 240 fps. Click **Start live stream**. The video window opens on the receiving computer when frames arrive.
7. To change the limit, select **Stop live stream**, edit the value and start the stream again. The same GStreamer command can continue listening. Use the same procedure to change the receiver address or port, and update the receiver command if its address family or port changes. To finish, stop the stream and press **Ctrl+C** in the receiver terminal. Closing the receiver or browser leaves the sender running until an operator stops it or the service restarts.

For an IPv6 destination, the generated command includes `address=::` on `udpsrc`. For a link-local address, include the Pi's outgoing interface in **Receiver IP**, for example `fe80::1234%eth0`. Use the same UDP port in the receiver and the console. The sender supports one live destination. Transmit on a trusted LAN or inside a VPN because the media packets themselves are unencrypted.

## What determines the frame rate and latency

Each frame is compressed independently. The sender uses the exposure timestamp from the camera request for its 90 kHz RTP timestamp. Adjacent frames at exactly 240 fps are 375 ticks apart. JPEG fragments fit into UDP payloads of at most 1200 bytes. An encoder/network worker can hold one frame in progress and one waiting frame. A new frame replaces a waiting frame if the worker is behind. The receiver requests a 1 MiB UDP socket buffer so short bursts can be drained between scheduler wakeups. The kernel may cap the actual size. On Linux, inspect the receiver's limit with `sysctl net.core.rmem_max` if GStreamer reports a smaller allocation or drops persist. After RTP reassembly a one-frame leaky queue discards older complete JPEGs when decoding falls behind, letting the network thread continue to service the socket. The receiver command omits an explicit jitter buffer and `sync=false` displays a decoded frame without waiting for its scheduled presentation time. [RTP/JPEG specification](https://datatracker.ietf.org/doc/html/rfc2435) · [GStreamer RTP/JPEG receiver](https://gstreamer.freedesktop.org/documentation/rtp/rtpjpegdepay.html)

End-to-end delay includes exposure, readout, JPEG encoding, packet transit, decoding and display refresh. At 240 fps one frame period is about 4.17 ms. A literal zero-millisecond delay is physically unattainable. A slower display can accept decoded frames faster than it can present distinct images.

Use the **Stream** sender bitrate as a starting point for capacity measurements. For example, 30,000 JPEG bytes/frame at 240 fps require 57.6 Mbit/s before protocol overhead. JPEG size depends on scene content. Verify with a detailed moving scene over the intended Ethernet or Wi-Fi link.

## When playback stutters

Under **Stream performance**, compare **Capture rate** and **Sender rate** while the problem is visible. A message indicates whether the sender has replaced queued frames or dropped frames due to a busy network socket. Open **Sender diagnostics** to see which counters are still rising. Each stream starts with fresh counters.

| Counter | What an increase means |
| --- | --- |
| Skipped by FPS limit | The sender intentionally skipped a captured frame before queuing it. These skips are counted separately from **Sender drops**. |
| Queue replacements | A newer frame replaced one waiting while the worker was encoding or sending the previous JPEG. |
| UDP retries | The Pi's UDP socket temporarily could not accept a packet. The sender retried the same fragment. A retry can succeed with the whole frame delivered to the socket. |
| Network congestion drops | The socket remained busy beyond the retry window for a JPEG. The sender abandoned that frame and moved on. |

If **Capture rate** stays near the camera setting while queue replacements or congestion drops climb, try a lower **Stream FPS limit**, such as 60 for a 240 fps capture. This reduces JPEG encoding and network load. If the sender keeps up and the picture still stutters, measure decoded FPS on the receiving computer using the command below. **Frames sent** counts complete frames submitted to the Pi's socket. Packets can still be lost on the link, and decoding or display can also limit playback.

## Acceptance on the intended link

1. After warmup, record the requested capture rate, stream FPS limit, measured capture and sender rates, bitrate and duration. Take a reading of the camera and sender counters at each end of the interval. Record CPU temperature and whether recording and preview were active.
2. Check for the optional FPS counter with `gst-inspect-1.0 fpsdisplaysink`. On Debian and Ubuntu, install it with `sudo apt install gstreamer1.0-plugins-bad`. Stop the display receiver with **Ctrl+C** to release the UDP port. Run the following receiver to count decoded frames independently of display refresh. The `current` and `average` rates appear in the terminal.

```sh
gst-launch-1.0 -v udpsrc port=5004 buffer-size=1048576 caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload=26" ! rtpjpegdepay ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! jpegdec ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false
```

3. Test Ethernet and Wi-Fi separately. Include scene detail, resolution, sensor mode, packet loss, duration and receiver FPS in each result. For a glass-to-glass delay measurement, film a changing LED or on-screen counter and the receiving display together with an independent fast camera.
4. If the live image breaks up while capture and sender rates remain high, verify the receiver's UDP firewall and CPU load, and check the link for drops. On a reordering link, insert `rtpjitterbuffer latency=10 drop-on-latency=true !` before `rtpjpegdepay` to allow up to 10 ms of reordering at the cost of added latency.
