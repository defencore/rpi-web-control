# Convert a recording on a Mac or PC

The desktop exporter reads a session ZIP or a completed session directory and creates an H.264 MP4 with a frame index and timestamp on each image. It can also overlay telemetry matched to each exposure, including in a 240 fps recording.

## Download the session

Stop all recording sources for the clip, open **Recordings**, and choose **Download recording (.zip)** on that session. The archive contains three files at its top level. `session.json` describes the camera mode and clocks. `frames.csv` identifies each saved frame, its byte range and its timestamps in nanoseconds. The original pixels are in `video.mjpeg` or `video.y4m`.

The ZIP can be passed directly to the exporter. If you already have the three files in a directory, pass that directory instead. The **Individual files** links support resuming large downloads. [Recording sources and downloads](storage.md)

## Convert a ZIP to MP4

On **Recordings**, expand **Convert to MP4** and download `render_recording.py` alongside the ZIP. The script also ships in the project checkout under `tools/`. On the workstation, use Python 3.11 or newer, Pillow 9.4 or newer, and FFmpeg with `ffprobe` and the `libx264` encoder. Sensor-timed playback requires the `fsync` filter. On a Mac with Homebrew, run this setup once from a working directory:

```sh
brew install python ffmpeg
python3 -m venv .export-venv
. .export-venv/bin/activate
python3 -m pip install 'Pillow>=9.4'
ffmpeg -hide_banner -filters | grep -w fsync
```

If the converter reports `No module named 'PIL'`, activate the export environment and run `python3 -m pip install Pillow` there. Pillow provides the `PIL` module used to draw the timestamps.

With the virtual environment active, substitute the name of the downloaded ZIP and choose a new output filename:

```sh
python3 "$HOME/Downloads/render_recording.py" "$HOME/Downloads/20261119T120000Z-012345abcdef.zip" capture.mp4
```

For ZIP input, playback follows the measured time between exposures. A pause in capture appears as a held image in the MP4. If you use the project checkout, run the same command with `tools/render_recording.py` as the script. For a session directory, add `--timing sensor`:

```sh
python3 tools/render_recording.py ./captures/20261119T120000Z-012345abcdef capture-timed.mp4 --timing sensor
```

Open the MP4 with `open capture.mp4` on macOS. Keep the original ZIP for the full nanosecond timestamps and original camera frames.

## Copy a session over SSH

To transfer a directory over SSH, use a login with read and traversal permission for the selected recording. SSH must be enabled on the Pi. Substitute the login, device name and session directory below. For a managed drive, use `/srv/rpi-web-control-nvme/recordings` or `/srv/rpi-web-control-usb/recordings` in the remote path.

```sh
PI=operator@raspberrypi.local
SESSION=20261118T120000Z-012345abcdef
mkdir -p "./captures/$SESSION"
rsync -rt --partial --progress "$PI:/var/lib/rpi-web-control/recordings/$SESSION/" "./captures/$SESSION/"
```

For a machine with OpenSSH only, `scp -r operator@raspberrypi.local:/path/to/session ./captures/` copies a directory. If the login cannot read the service-owned directory, a board administrator can stage just the chosen session. On the Pi, allow enough space for the copy.

```sh
SESSION=20261118T120000Z-012345abcdef
mkdir -m 700 -p "$HOME/capture-export"
sudo cp -a -- "/var/lib/rpi-web-control/recordings/$SESSION" "$HOME/capture-export/"
sudo chown -R "$(id -u):$(id -g)" "$HOME/capture-export/$SESSION"
```

For a staged copy, use `"$PI:capture-export/$SESSION/"` as the rsync source. Use the login's SSH credentials for this transfer.

## Choose a constant playback rate

The exporter also supports constant-rate playback. It defaults to this mode for session directories. You can select it explicitly for a ZIP or directory. The example assumes `SESSION` names the session directory copied above.

```sh
python3 tools/render_recording.py "./captures/$SESSION" run-240.mp4 --timing constant --text "Test run 07"
```

In constant mode the playback rate comes from the camera settings in the manifest. The example creates a 240 fps MP4 for a 240 fps capture profile. A clip recorded at 30 fps defaults to 30 fps. To inspect a 240 fps capture at quarter speed, add `--fps 60`. `--fps 30000/1001` sets a rational output rate.

The OSD shows the zero-based frame index and selected timestamp in nanoseconds. Text appears in a band along the bottom. `--text` adds a static label. Use short field labels for small images. Supply a TrueType font with `--font /path/to/font.ttf` for additional glyphs.

## Align telemetry

Supply a UTF-8 CSV with ascending integer timestamps in the **same clock domain and units** as the selected frame clock. For example, the following samples use the Pi's Linux monotonic clock in nanoseconds. Replace the values with real samples from the recording interval.

```csv
monotonic_ns,rpm,temperature_c
122501400000,1430,36.1
122505600000,1470,36.2
122514900000,1510,36.3
```

```sh
python3 tools/render_recording.py "./captures/$SESSION" run-telemetry.mp4 --timing sensor \
  --telemetry telemetry.csv --clock monotonic_ns --field rpm=RPM \
  --field "temperature_c=Temp C" --max-age-ms 100 --text "Test run 07"
```

| Frame clock | Matching input |
| --- | --- |
| `monotonic_ns` | Linux CLOCK_MONOTONIC from the same device boot. For the systemd journal, multiply `__MONOTONIC_TIMESTAMP` by 1000. |
| `sensor_ns` | Linux CLOCK_BOOTTIME on hardware, from the same device boot. |
| `realtime_ns` | Unix epoch nanoseconds. For external logging equipment, establish synchronization and measure its offset first. |

An optional telemetry column named `boot_id` is checked against the session when a boot ID is available. Journal boot IDs can be written with or without hyphens. If the incoming timestamp column is called `time_ns`, add `--time-column time_ns`. This changes the column name only. The collector must still supply the selected frame clock. The exporter reports an error if the chosen frame clock runs backwards, for example after a wall-clock correction.

Each frame uses the latest telemetry sample at or before its timestamp. A sample exactly at the age limit is still valid. Before the first sample or after the maximum age, the overlay displays `--`. The default age limit is 1000 ms. Repeat `--field COLUMN=LABEL` to select channels and their display order. With telemetry and no field list, the exporter displays all data columns.

## Timing and verification

Each output frame comes from the byte range indexed for that source frame. The exporter checks the completed manifest and frame boundaries, renders the overlay, and checks the encoded stream with ffprobe before publishing the new MP4. Choose a new output filename for each export.

With `--timing sensor`, each saved frame appears once. Its MP4 presentation timestamp is the elapsed `sensor_ns` since the first saved frame, rounded to the nearest microsecond. The last frame lasts one nominal capture period. `--clock` selects the timestamp printed on each frame and used for telemetry matching. For example, `--clock realtime_ns` displays Unix epoch nanoseconds while sensor timestamps still determine the playback intervals.

With `--timing constant`, the exporter spaces frames at the playback rate. Under the default `--gap-policy compact`, missing capture intervals shorten this timeline. The OSD timestamps still follow the original capture. The summary reports how many sensor intervals exceeded 1.5 nominal periods. `--gap-policy reject` stops the export if such a gap occurs. Use the original index for nanosecond measurements.

```sh
ffprobe -v error -select_streams v:0 -count_frames \
  -show_entries stream=codec_name,width,height,avg_frame_rate,time_base,nb_read_frames \
  -of json run-telemetry.mp4
ffplay run-telemetry.mp4
```

Desktop display refresh, video player and decoder performance determine how many frames are visible during real-time playback. Frame stepping or a slower playback export helps inspect individual events.
