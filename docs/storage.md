# Recording media

Open **Recording drives → Recording destination**. **Current destination** shows the selected medium, the directory for new clips and its remaining space. Use **View recordings in this folder** to find clips saved there. Under **Choose where to save new recordings**, choose **Use NVMe** for a ready PCIe SSD, **Use USB** for a ready USB flash drive, or another available recording volume. The choice takes effect immediately. To use a subdirectory, expand **Use a custom folder**, enter its absolute path under an allowed root, and select **Save destination**. [See the drive selector](interface-guide.md#choose-where-to-save)

After connecting a drive, use **Refresh drives** to check the available destinations. Expand **Detected drives and partitions** for device names, mountpoints and capacities. If the active directory shows **Unavailable**, check the displayed error and verify the connection and mount before recording. [Troubleshoot a recording I/O error](#troubleshoot-a-recording-io-error)

| Medium | Appearance | Recording destination |
| --- | --- | --- |
| Native NVMe | SSD model, serial, disk size and partitions under **Set up a PCIe NVMe drive** | Use the managed ext4 volume or an administrator-provisioned recording root |
| USB flash or SSD enclosure | Drive model, capacity and partitions under **Set up a USB drive** | A managed ext4 USB volume or an administrator-provisioned recording root |
| microSD / eMMC | SD and MMC card types reported by the kernel | The default recording directory when it lives on the system card, or a separate mounted data partition |
| SPI NOR or NAND presented as Linux MTD | Raw MTD device and its name. A mounted JFFS2 filesystem or UBIFS volume appears separately. | A writable, administrator-provisioned data filesystem |
| Unmounted device | Hardware type and block capacity when available | Mount the filesystem and grant the service account access before selecting it |

A raw MTD node is inventory only. The web preparation controls apply to eligible PCIe NVMe and USB mass-storage disks. An SPI peripheral exposed solely through a generic SPI interface has no block device or MTD information for the storage inventory. Bootloader flash is firmware storage. For SPI recording media verify usable capacity, endurance and sustained throughput on the exact chip and filesystem. A 640 × 480 YUV420 stream at 240 fps requires approximately 111 MB/s before metadata and filesystem overhead.

## Record to USB or PCIe NVMe

For a ready drive, choose its destination button, check **Current destination**, then start recording in **Capture monitor**.

| Drive | Mount or initialize it under **Recording drives** | Select it for recording | Managed recording directory |
| --- | --- | --- | --- |
| USB flash drive or USB SSD | **Set up a USB drive** | **Use USB** | `/srv/rpi-web-control-usb/recordings` |
| PCIe NVMe SSD | **Set up a PCIe NVMe drive** | **Use NVMe** | `/srv/rpi-web-control-nvme/recordings` |

If the drive needs mounting or preparation, finish active recordings and expand the corresponding setup section. Refresh the list and identify the disk by its model, capacity and displayed device path. The USB setup accepts directly attached USB mass-storage drives using Linux usb-storage or UAS. The controller excludes every physical disk backing the Pi's system or boot partition, including the other partitions on each disk. To verify both, it needs to identify the root filesystem and the separate FAT boot filesystem mounted at `/boot/firmware`. If it cannot verify either, disk setup and cleanup are unavailable.

- For an **existing ext4 partition**, choose **Mount existing volume** to keep its files. The partition needs a usable, unique filesystem UUID.
- For a **drive you intend to erase**, open the erase controls, enter the displayed disk path and select **Erase disk and create ext4 volume**. **This erases every partition and file on the selected disk.** The helper creates a GPT, a partition and an ext4 filesystem. A failed operation after partitioning can leave a partially prepared disk. Refresh its inventory before deciding whether to retry.

Once the volume is ready, choose **Use USB** or **Use NVMe** to make it the recording destination. After reboot the system uses the filesystem UUID in the managed `/etc/fstab` entry to mount the drive. If the drive is absent, its recording destination is unavailable. For a USB drive, check the mount with `findmnt --mountpoint /srv/rpi-web-control-usb`. For a PCIe SSD, use `findmnt --mountpoint /srv/rpi-web-control-nvme`.

The package enables the managed recording root on upgrade, including installations with customized storage roots. Manual volume management remains available for existing mounts, other filesystems and other media. [Debian fstab](https://manpages.debian.org/trixie/mount/fstab.5.en.html) · [Debian sfdisk](https://manpages.debian.org/trixie/fdisk/sfdisk.8.en.html)

## Download a recording

Open **Recordings**. The **Folder to browse** selector changes only the list you are viewing. Choose the current destination, the default recording directory (the first configured root, usually on the system card), or the top-level recordings directory on a managed USB or NVMe drive. Each distinct directory appears once. For example, while recording to NVMe, select **Default recording folder** to find an earlier clip on the system card. A clip in progress appears in the list after it finishes. Each row shows the recording ID, start time in the browser’s time zone, video format, saved frame count and total file size. Use **Refresh recordings** to reload the list manually.

In **Find recordings**, paste a full recording ID from the Pi to look it up anywhere in the selected folder. This also works when the Pi clock was wrong. A partial ID, date or format filters only the loaded recordings. Choose **Load older recordings** to extend that search or **Clear filter** to return to the folder.

Select **Download ZIP** on a completed session. The ZIP contains the original `video.y4m` (high-speed YUV420) or `video.mjpeg` (Motion JPEG), `frames.csv` with per-frame timestamps, and `session.json`. On your computer, pass the ZIP directly to the MP4 exporter. [Convert a recording on a Mac or PC](post-processing.md)

Open **Files and actions** to retrieve just the video, index or manifest. Individual file downloads support HTTP byte ranges for resuming an interrupted transfer. An interrupted ZIP download restarts from the beginning. Both use HTTPS and follow the console's token setting. Refresh the list if the source changes.

## Troubleshoot a recording I/O error

An `[Errno 5] Input/output error` with a session directory name means a filesystem operation failed. It can appear while creating or finalizing a clip, or while reading a directory to display recordings. The error alone does not identify the device or tell you how much of the clip was written.

1. Note the exact session name, the time, and **Recording drives → Current destination**. In **Capture monitor**, check whether a recording is active or waiting to resume. Use **Stop & pause triggers** if a rule still holds it. While investigating storage you can pause the automatic rules from **Recording triggers** as well. In earlier versions, disable the active rule and release any manual hold. [Recording controls](triggers.md#input-timing-and-troubleshooting)
2. From the workstation checkout, run `./install.sh --diagnose ubuntu@192.168.55.100` using the board's SSH login and address. The report works offline and is read-only. In `diagnostics.txt`, look under **Recording drive faults (previous boot, visible to this account)** if the drive came back after a reboot. Each boot has a separate section with up to 12 matching controller or transport messages and 12 matching filesystem or I/O errors. If the Pi discarded the journal at reboot or your SSH account cannot read it, the report marks that boot’s kernel journal as unavailable. Keep the report with the session name.
3. On the Pi, use the commands below to compare the intended volume against the mounted device and inspect recent errors. For USB, replace the NVMe mountpoint with `/srv/rpi-web-control-usb`. For the system card or a custom location, use `findmnt --target /path/to/active/directory` with the actual directory.

   ```sh
   lsblk -o NAME,TRAN,MODEL,FSTYPE,SIZE,MOUNTPOINTS
   findmnt --mountpoint /srv/rpi-web-control-nvme -o TARGET,SOURCE,FSTYPE,OPTIONS
   journalctl -b -u rpi-web-control.service -n 100 --no-pager
   sudo journalctl -k -b -n 150 --no-pager
   ```

Check for a missing mount or storage resets near the time of the failure. An NVMe controller reset failure, a drive suddenly reporting **0 B**, an aborted ext4 journal, or mount options containing `emergency_ro` or `shutdown` are reasons to keep recording stopped on that volume. Preserve the session directory and copy any readable `video.*`, `frames.csv` and `session.json` files together before attempting repairs. After the expected volume is available again and reports its normal capacity, make a short test clip and verify it appears as complete in **Recordings**.

## Delete recordings and free space

In **Recordings**, choose the managed USB or NVMe directory you want to clean. Finish active recordings and release recording triggers first. To remove one completed session, open **Files and actions** on its row and choose **Delete recording…**. To review eligible completed sessions across the displayed directory, clear the search and open **Delete recordings from this folder → Review all recordings for deletion…**. Review the device, directory, number of sessions and total bytes. Type `DELETE` for one session or `CLEAN` for the directory, then confirm. Choose **Cancel** to close the review. Deleting a session removes its video, frame index and manifest together.

Cleanup is limited to verified controller recordings on managed external volumes. The Pi's system disk, its other partitions, unrelated files and incomplete recordings are excluded. If the drive or the selected files change after review, request a fresh review. To preserve a clip, download its ZIP before confirming deletion.

## Provision a custom volume

1. Provision a stable OS mountpoint, for example `/srv/camera-ssd`. For persistent mounts use the filesystem UUID in `/etc/fstab`. Check the actual source with `findmnt --target /srv/camera-ssd`.
2. With the volume mounted, provision a writable subdirectory on it. For example, `sudo install -d -o rpi-web-control -g rpi-web-control -m 0750 /srv/camera-ssd/recordings`.
3. Add that directory to `RPI_CONTROL_STORAGE_ROOTS` in `/etc/default/rpi-web-control`. Entries are absolute paths separated by colons. For manually mounted media, set `RPI_CONTROL_REQUIRED_MOUNTS` to its mountpoint. For example:

   ```ini
   RPI_CONTROL_STORAGE_ROOTS=/var/lib/rpi-web-control/recordings:/srv/camera-ssd/recordings
   RPI_CONTROL_REQUIRED_MOUNTS=/srv/camera-ssd
   ```

4. Restart the service after changing the environment or fstab. Stop any active recording and open **Recording drives**. Choose the mounted recording root, or enter a subdirectory under **Use a custom folder** and select **Save destination**. A data directory on the root filesystem, including a system microSD card, works without an additional mount requirement.

The service honors matching mountpoints from fstab and the explicit required-mount list. This lets it detect an unplugged drive even before the first recording. For an existing configuration, the first recording or explicit destination selection saves the filesystem identity. A registered filesystem UUID allows reconnection even when Linux assigns another block-device name. If no UUID, partition UUID or persistent device ID is available, the identity can be specific to the current boot and mount. Deliberately save the destination again after verifying the intended media.

Recording opens the selected directory, creates the session beneath its open file descriptor, and retains a descriptor to that session for the manifest, frame index and video. A removed mount cannot redirect those writes to a similarly named underlying directory. On write failure the session may be incomplete. An active recording source retries against the saved volume identity. Reconnecting the same volume can resume capture in a new session.

The service reads its own mount namespace and the device topology available through Linux sysfs. Device mapper and layered storage may appear under their underlying transport when the backing devices agree. A card in a USB reader can appear as USB. Only media exposed by the kernel can be inventoried.

[Linux mount information](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html) · [Kernel sysfs device links](https://docs.kernel.org/filesystems/sysfs.html) · [UBI attributes](https://docs.kernel.org/6.15/admin-guide/abi-stable.html)
