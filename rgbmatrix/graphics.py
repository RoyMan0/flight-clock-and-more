"""
rgbmatrix.graphics stub — no-op implementations of Font, Color, DrawText, DrawLine.

On a real Pi the C extension provides these. Here they are pure-Python stubs
that allow the code to import and run without hardware.
"""


class Color:
    def __init__(self, r=0, g=0, b=0):
        self.red   = r
        self.green = g
        self.blue  = b

    def __repr__(self):
        return f"Color({self.red}, {self.green}, {self.blue})"


class Font:
    def __init__(self):
        self.height = 8
        self.baseline = 6

    def LoadFont(self, path: str):
        pass  # no-op; font rendering is skipped in software mode

    def CharacterWidth(self, char_code: int) -> int:
        return 6  # reasonable default for most BDF fonts


def DrawText(canvas, font, x, y, color, text) -> int:
    """No-op stub. Returns estimated pixel width of the text."""
    try:
        width = sum(font.CharacterWidth(ord(c)) for c in text)
    except Exception:
        width = len(text) * 6
    return width


def DrawLine(canvas, x1, y1, x2, y2, color):
    pass  # no-op; line rendering is skipped in software mode


def DrawCircle(canvas, x, y, radius, color):
    pass
