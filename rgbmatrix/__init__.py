"""
rgbmatrix stub — used on non-Pi systems (Mac dev, CI) when the real
rpi-rgb-led-matrix C extension is not installed.

Provides just enough surface area for the code to import and run in
--no-hardware mode. All hardware operations are no-ops.
"""


class RGBMatrixOptions:
    def __init__(self):
        self.hardware_mapping = "adafruit-hat"
        self.rows = 32
        self.cols = 64
        self.chain_length = 1
        self.parallel = 1
        self.row_address_type = 0
        self.multiplexing = 0
        self.pwm_bits = 11
        self.brightness = 100
        self.pwm_lsb_nanoseconds = 130
        self.led_rgb_sequence = "RGB"
        self.pixel_mapper_config = ""
        self.show_refresh_rate = 0
        self.gpio_slowdown = 2
        self.disable_hardware_pulsing = False
        self.drop_privileges = True


class _StubCanvas:
    def Clear(self): pass
    def SetPixel(self, x, y, r, g, b): pass


class RGBMatrix:
    def __init__(self, options=None):
        self.brightness = 100

    def CreateFrameCanvas(self):
        return _StubCanvas()

    def SwapOnVSync(self, canvas):
        return canvas
