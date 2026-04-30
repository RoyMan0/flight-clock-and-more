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


class DisplayManager:
    WIDTH = 64
    HEIGHT = 32

    def __init__(self, config: dict, software_mode: bool = False):
        self.software_mode = software_mode
        self._brightness = config.get("brightness", 100)
        self._lock = threading.Lock()

        # PIL mirror — always maintained for web dashboard snapshots
        self._pil_image = Image.new("RGB", (self.WIDTH, self.HEIGHT), (0, 0, 0))

        if software_mode:
            self.matrix = None
            self.canvas = _SoftwareCanvas(self.WIDTH, self.HEIGHT, self._pil_image)
        else:
            self._init_hardware(config)

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
        options.pwm_bits = 11
        options.brightness = self._brightness
        options.pwm_lsb_nanoseconds = 130
        options.led_rgb_sequence = "RBG"
        options.pixel_mapper_config = ""
        options.show_refresh_rate = 0
        options.gpio_slowdown = config.get("gpio_slowdown", 2)
        options.disable_hardware_pulsing = True
        options.drop_privileges = True

        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()
        self.canvas.Clear()

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
        if self.software_mode:
            self.canvas._flush_to_pil(self._pil_image)
        else:
            self.matrix.SwapOnVSync(self.canvas)

    def clear(self):
        self.canvas.Clear()
        with self._lock:
            self._pil_image = Image.new("RGB", (self.WIDTH, self.HEIGHT), (0, 0, 0))

    # ------------------------------------------------------------------
    # Brightness
    # ------------------------------------------------------------------

    def set_brightness(self, value: int):
        value = max(0, min(100, value))
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
        with self._lock:
            return self._pil_image.copy()



class _SoftwareCanvas:
    """Minimal shim that mimics rgbmatrix FrameCanvas for software mode."""

    def __init__(self, width: int, height: int, pil_image: Image.Image):
        self.width = width
        self.height = height
        self._pixels: list[list[tuple]] = [
            [(0, 0, 0)] * width for _ in range(height)
        ]
        self._pil_ref = pil_image

    def Clear(self):
        for row in self._pixels:
            for i in range(len(row)):
                row[i] = (0, 0, 0)

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int):
        if 0 <= x < self.width and 0 <= y < self.height:
            self._pixels[y][x] = (r, g, b)

    def _flush_to_pil(self, img: Image.Image):
        for y, row in enumerate(self._pixels):
            for x, pixel in enumerate(row):
                img.putpixel((x, y), pixel)
