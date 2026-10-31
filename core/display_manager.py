"""
Thin wrapper around the RGB matrix hardware (or a PIL software canvas when
running without hardware). Every plugin receives a reference to this object
and draws to self.canvas, then the plugin_manager calls swap() each frame.

Software mode (--no-hardware flag) renders to a PIL Image instead of the
LED panel — useful for development on a Mac or testing without the Pi.
"""

import threading
from typing import Optional
from PIL import Image, ImageDraw

from core import canvas_mirror

canvas_mirror.install()


class DisplayManager:
    WIDTH = 64
    HEIGHT = 32

    def __init__(self, config: dict, software_mode: bool = False):
        self.software_mode = software_mode
        self._brightness = int(config.get("brightness", 100))
        self._lock = threading.Lock()

        # Legacy PIL image some plugins still paste into; the live preview now comes
        # from the canvas mirror below, which sees every pixel drawn on the panel.
        self._pil_image = Image.new("RGB", (self.WIDTH, self.HEIGHT), (0, 0, 0))

        if software_mode:
            self.matrix = None
            real = _SoftwareCanvas(self.WIDTH, self.HEIGHT, self._pil_image)
        else:
            real = self._init_hardware(config)
        # Plugins draw on this wrapper; it forwards to the panel and keeps a copy for the web preview
        self.canvas = canvas_mirror.MirrorCanvas(real, self.WIDTH, self.HEIGHT)

    def _init_hardware(self, config: dict):
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError:
            raise RuntimeError(
                "rgbmatrix library not found. Run with --no-hardware for software mode."
            )

        options = RGBMatrixOptions()
        options.hardware_mapping = (
            "adafruit-hat-pwm" if config.get("hat_pwm_enabled", False)
            else "adafruit-hat"
        )
        options.rows = self.HEIGHT
        options.cols = self.WIDTH
        options.chain_length = 1
        options.parallel = 1
        options.row_address_type = 0
        options.multiplexing = 0
        options.pwm_bits = config.get("pwm_bits", 11)
        options.brightness = self._brightness
        options.pwm_lsb_nanoseconds = config.get("pwm_lsb_nanoseconds", 130)
        options.led_rgb_sequence = config.get("led_rgb_sequence", "RBG")
        options.pixel_mapper_config = ""
        options.show_refresh_rate = 0
        options.gpio_slowdown = config.get("gpio_slowdown", 2)
        options.disable_hardware_pulsing = False
        options.drop_privileges = True

        self.matrix = RGBMatrix(options=options)
        canvas = self.matrix.CreateFrameCanvas()
        canvas.Clear()
        return canvas

    # ------------------------------------------------------------------
    # Frame control
    # ------------------------------------------------------------------

    def swap(self):
        """Push current canvas to display.

        Deliberately does NOT capture SwapOnVSync's return value, matching the
        original its-a-plane.py behaviour. The scenes draw incrementally on a
        single persistent canvas object; SwapOnVSync just pushes it to the
        hardware each frame. Capturing the return value would introduce true
        double-buffering which causes the scenes to flicker because they only
        update one buffer per N frames.
        """
        if not self.software_mode:          # software mode: the canvas mirror already holds the frame
            self.matrix.SwapOnVSync(self.canvas.real)

    def clear(self):
        self.canvas.Clear()
        with self._lock:
            self._pil_image = Image.new("RGB", (self.WIDTH, self.HEIGHT), (0, 0, 0))

    # ------------------------------------------------------------------
    # Brightness
    # ------------------------------------------------------------------

    def set_brightness(self, value: int):
        value = max(0, min(100, int(value)))
        if self._brightness == value:
            return
        self._brightness = value
        if not self.software_mode and self.matrix:
            self.matrix.brightness = value

    @property
    def brightness(self) -> int:
        return self._brightness

    # ------------------------------------------------------------------
    # PIL snapshot (for web dashboard)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Image.Image:
        """Exactly what's on the panel right now (from the canvas mirror)."""
        return self.canvas.snapshot()



class _SoftwareCanvas:
    """Stands in for the rgbmatrix FrameCanvas in software mode.

    Drawing is a no-op: MirrorCanvas keeps the real 64×32 copy (used by the web
    preview and the Mac app's panel window), so keeping a second per-pixel copy
    here only cost ~2,000 Python calls per frame.
    """

    def __init__(self, width: int, height: int, pil_image: Image.Image = None):
        self.width = width
        self.height = height

    def Clear(self):
        pass

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int):
        pass

    def Fill(self, r, g, b):
        pass

    def SetImage(self, image, offset_x=0, offset_y=0, *args, **kwargs):
        pass
