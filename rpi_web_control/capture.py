"""Camera sources hand complete requests to one consumer; web clients never own buffers."""

import io
import math
import queue
import threading
import time


def boot_ns():
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    return time.monotonic_ns()


def select_mode(modes, settings):
    depth = settings.get("sensor_bit_depth", 0)
    supported = [
        m for m in modes if m["width"] >= settings["width"]
        and m["height"] >= settings["height"] and m["max_fps"] >= settings["fps"]
        and (depth == 0 or m["bit_depth"] == depth)
    ]
    if not supported:
        raise ValueError("No sensor mode supports the requested resolution, frame rate and bit depth.")
    return min(supported, key=lambda m: (m["width"] * m["height"], -m["bit_depth"]))


def as_jpeg(image):
    out = io.BytesIO()
    image.save(out, "JPEG", quality=82)
    return out.getvalue()


def overlay(image, timestamp):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    label = f"{timestamp // 1_000_000_000}.{timestamp % 1_000_000_000:09d} s"
    box = draw.textbbox((6, 4), label)
    draw.rectangle((2, 2, box[2] + 4, box[3] + 3), fill="black")
    draw.text((6, 4), label, fill="white")


def yuv_planes(data, width, height, stride):
    """Return visible planar samples, excluding ISP alignment padding."""
    length = height * stride
    if len(data) < length * 3 // 2:
        raise ValueError("Camera returned a truncated YUV plane.")
    return (
        data[:length].reshape(height, stride)[:, :width],
        data[length:length * 5 // 4].reshape(height // 2, stride // 2)[:, :width // 2],
        data[length * 5 // 4:length * 3 // 2].reshape(height // 2, stride // 2)[:, :width // 2],
    )


class PiFrame:
    def __init__(self, request):
        self.request = request
        self.sensor_ns = int(request.get_metadata()["SensorTimestamp"])

    def acquire(self):
        self.request.acquire()

    def release(self):
        self.request.release()

    def jpeg(self, osd):
        if osd:
            image = self.request.make_image("main")
            overlay(image, self.sensor_ns)
            return as_jpeg(image)
        # Picamera2 encodes YUV planes directly with libjpeg-turbo.
        out = io.BytesIO()
        self.request.save("main", out, format="jpeg")
        return out.getvalue()

    def live_jpeg(self, osd):
        if osd:
            return self.jpeg(True)
        from picamera2 import MappedArray
        from simplejpeg import encode_jpeg_yuv_planes

        # Encode only the visible YUV samples. EXIF and file serialization are
        # unnecessary for RTP, which transmits the JPEG entropy scan and quantizers.
        cfg = self.request.config["main"]
        width, height = cfg["size"]
        stride = cfg["stride"]
        with MappedArray(self.request, "main", reshape=False) as mapped:
            # Temporary plane views must expire before the camera mapping closes.
            return encode_jpeg_yuv_planes(*yuv_planes(mapped.array, width, height, stride), 82)

    def luma_preview(self):
        from picamera2 import MappedArray
        from PIL import Image

        width, height = self.request.config["main"]["size"]
        step = max(1, math.ceil(width / 640), math.ceil(height / 480))
        with MappedArray(self.request, "main") as mapped:
            # Copy only the decimated luma plane before releasing the camera buffer.
            return Image.fromarray(mapped.array[:height:step, :width:step].copy())

    def write_yuv(self, file):
        from picamera2 import MappedArray

        cfg = self.request.config["main"]
        width, height = cfg["size"]
        stride = cfg["stride"]
        with MappedArray(self.request, "main", reshape=False) as mapped:
            if stride == width:
                size = height * width * 3 // 2
                if len(mapped.array) < size:
                    raise ValueError("Camera returned a truncated YUV plane.")
                file.write(memoryview(mapped.array)[:size])
            else:
                file.writelines(plane.tobytes() for plane in yuv_planes(mapped.array, width, height, stride))


class PiSource:
    def __init__(self):
        try:
            from picamera2 import Picamera2
            self.camera = Picamera2()
        except (ImportError, RuntimeError, IndexError) as exc:
            raise RuntimeError(f"Camera unavailable: {exc}") from exc
        self.requests = queue.Queue(4)
        self.guard = threading.Lock()
        self.accepting = False
        self.queue_drops = 0
        self.running = False

    def capabilities(self):
        modes = [{"width": m["size"][0], "height": m["size"][1],
                  "bit_depth": m["bit_depth"], "max_fps": m["fps"]}
                 for m in self.camera.sensor_modes if all(k in m for k in ("size", "bit_depth", "fps"))]
        if not modes:
            raise RuntimeError("The camera does not report CSI sensor modes.")
        properties = self.camera.camera_properties
        details = {}
        for key, label in (("PixelArraySize", "pixel_array_size"), ("UnitCellSize", "unit_cell_size_nm")):
            size = properties.get(key)
            if size is not None:
                pair = (size.width, size.height) if hasattr(size, "width") else tuple(size)
                details[label] = list(map(int, pair))
        if "Rotation" in properties:
            details["rotation_degrees"] = int(properties["Rotation"])
        if "Location" in properties:
            location = int(properties["Location"])
            details["location"] = {0: "Front", 1: "Back", 2: "External"}.get(location, str(location))
        return {"available": True, "model": str(properties.get("Model", "Camera")),
                "properties": details, "modes": modes, "error": None}

    def _receive(self, request):
        with self.guard:
            if not self.accepting:
                return
            if self.requests.full():
                self.queue_drops += 1
                return
            request.acquire()
            self.requests.put_nowait(request)

    def start(self, settings, mode):
        size = (settings["width"], settings["height"])
        duration = round(1_000_000 / settings["fps"])
        pixel_format = "BGR888" if settings["osd"] and settings["fps"] <= 60 else "YUV420"
        config = self.camera.create_video_configuration(
            main={"size": size, "format": pixel_format},
            sensor={"output_size": (mode["width"], mode["height"]), "bit_depth": mode["bit_depth"]},
            raw=None, controls={"FrameDurationLimits": (duration, duration)}, buffer_count=8, queue=False,
        )
        self.camera.options["quality"] = 82
        self.camera.configure(config)
        actual = self.camera.camera_configuration()
        if tuple(actual["main"]["size"]) != size or actual["main"]["format"] != pixel_format:
            raise ValueError(f"The camera changed the requested {size} {pixel_format} output to {actual['main']}.")
        negotiated = actual.get("sensor") or {}
        if (tuple(negotiated.get("output_size", ())) != (mode["width"], mode["height"])
                or negotiated.get("bit_depth") != mode["bit_depth"]):
            raise ValueError(f"The camera could not apply the requested {mode['width']}x{mode['height']} "
                             f"{mode['bit_depth']}-bit sensor mode. Reported sensor configuration: {negotiated}.")
        self.camera.post_callback = self._receive
        self.accepting = True
        self.camera.start(show_preview=False)
        self.running = True

    def next_frame(self, timeout):
        try:
            request = self.requests.get(timeout=timeout)
        except queue.Empty:
            return None
        try:
            return PiFrame(request)
        except BaseException:
            request.release()
            raise

    def close(self):
        with self.guard:
            self.accepting = False
        try:
            if self.running:
                self.camera.stop()
        finally:
            while not self.requests.empty():
                self.requests.get_nowait().release()
            self.camera.close()


class SimFrame:
    def __init__(self, source):
        self.source = source
        self.sensor_ns = boot_ns()

    def acquire(self):
        pass

    def release(self):
        pass

    def jpeg(self, osd):
        if osd:
            image = self.source.image.copy()
            overlay(image, self.sensor_ns)
            return as_jpeg(image)
        return self.source.jpeg if self.source.jpeg is not None else as_jpeg(self.source.image)

    def live_jpeg(self, osd):
        return self.jpeg(osd)

    def luma_preview(self):
        image = self.source.image.convert("L")
        image.thumbnail((640, 480))
        return image

    def write_yuv(self, file):
        file.write(self.source.yuv)


class SimSource:
    queue_drops = 0

    def capabilities(self):
        modes = [{"width": w, "height": h, "max_fps": f, "bit_depth": 8}
                 for w, h, f in ((640, 480, 240), (1280, 720, 120), (1920, 1080, 60))]
        return {"available": True, "model": "Simulated camera", "modes": modes,
                "properties": {"pixel_array_size": [1920, 1080]}, "error": None}

    def start(self, settings, mode):
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:
            raise RuntimeError("The camera simulator requires Pillow (python3-pil).") from exc

        width, height = settings["width"], settings["height"]
        self.period = 1 / settings["fps"]
        self.deadline = time.monotonic()
        self.image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(self.image)
        colors = ("white", "yellow", "cyan", "green", "magenta", "red", "blue", "black")
        for i, color in enumerate(colors):
            draw.rectangle((width * i // 8, 0, width * (i + 1) // 8, height), fill=color)
        draw.rectangle((0, height * 3 // 4, width, height), fill="#252525")
        draw.text((8, height * 3 // 4 + 6), "SIMULATED CAMERA", fill="white")
        self.jpeg = as_jpeg(self.image) if settings["fps"] <= 60 else None
        if settings["fps"] > 60:
            y, u, v = self.image.convert("YCbCr").split()
            half = (width // 2, height // 2)
            self.yuv = y.tobytes() + u.resize(half).tobytes() + v.resize(half).tobytes()

    def next_frame(self, timeout):
        delay = self.deadline - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, timeout))
            if delay > timeout:
                return None
        self.deadline = max(self.deadline + self.period, time.monotonic())
        return SimFrame(self)

    def close(self):
        pass
