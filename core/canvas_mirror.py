"""
Canvas mirror — keeps an exact copy of every pixel drawn on the LED panel so the
web UI can show a live preview.

The rpi-rgb-led-matrix canvas can't be read back, and the older scenes draw
straight to it with graphics.DrawText / DrawLine / SetPixel, so the preview never
saw them. Two pieces fix that:

  MirrorCanvas   wraps the real canvas: every SetPixel/Clear/Fill/SetImage goes to
                 the hardware unchanged AND into a 64×32 RGB buffer.
  install()      wraps rgbmatrix.graphics.DrawText/DrawLine/DrawCircle and Font so
                 the C library still draws to the real canvas, then the same shapes
                 are drawn into the buffer in Python (glyph placement copied from
                 the library's Font::DrawGlyph, via the BDF data in utilities/textclip).

install() must run before any module creates graphics.Font objects (setup/fonts.py
does at import), so main.py calls it first. Mirroring is best-effort: any failure
in the mirror path is swallowed so the panel itself is never affected.
"""

import logging
import threading

from PIL import Image

log = logging.getLogger(__name__)

_installed = False
_font_cache = {}            # BDF path → utilities.textclip.ClipFont


class MirrorCanvas:
    """Transparent wrapper around a rgbmatrix FrameCanvas (or the software canvas)."""

    def __init__(self, real, width: int = 64, height: int = 32):
        self.real = real
        self._w, self._h = width, height
        self._buf = bytearray(width * height * 3)
        self._lock = threading.Lock()

    # --- drawing API used by plugins/scenes --------------------------
    def SetPixel(self, x, y, r, g, b):
        self.real.SetPixel(x, y, r, g, b)
        try:
            self.mirror_pixel(x, y, r, g, b)
        except Exception:
            pass                     # the preview must never break drawing

    def Clear(self):
        self.real.Clear()
        with self._lock:
            self._buf[:] = bytes(len(self._buf))

    def Fill(self, r, g, b):
        self.real.Fill(r, g, b)
        try:
            with self._lock:
                self._buf[:] = bytes((int(r) & 255, int(g) & 255, int(b) & 255)) * (self._w * self._h)
        except Exception:
            pass

    def SetImage(self, image, offset_x=0, offset_y=0, *args, **kwargs):
        """Fast whole-image push (C path on the Pi), mirrored with one paste."""
        img = image.convert("RGB")
        if hasattr(self.real, "SetImage"):
            self.real.SetImage(img, offset_x, offset_y, *args, **kwargs)
        else:                                             # software canvas
            px = img.load()
            for y in range(img.height):
                for x in range(img.width):
                    self.real.SetPixel(x + offset_x, y + offset_y, *px[x, y])
        try:
            snap = self.snapshot()
            snap.paste(img, (offset_x, offset_y))
            with self._lock:
                self._buf[:] = snap.tobytes()
        except Exception:
            pass

    def __getattr__(self, name):                         # width, height, anything else
        return getattr(self.real, name)

    # --- mirror helpers ----------------------------------------------
    def mirror_pixel(self, x, y, r, g, b):
        # Scenes sometimes pass floats (e.g. faded colours); the C library truncates, so do the same
        x, y = int(x), int(y)
        if 0 <= x < self._w and 0 <= y < self._h:
            i = (y * self._w + x) * 3
            self._buf[i] = int(r) & 255
            self._buf[i + 1] = int(g) & 255
            self._buf[i + 2] = int(b) & 255

    def snapshot(self) -> Image.Image:
        with self._lock:
            return Image.frombytes("RGB", (self._w, self._h), bytes(self._buf))


class _FontProxy:
    """Stands in for graphics.Font so we know which BDF file each font came from."""

    def __init__(self, real_cls):
        object.__setattr__(self, "_real", real_cls())
        object.__setattr__(self, "_path", None)

    def LoadFont(self, path):
        object.__setattr__(self, "_path", path)
        return self._real.LoadFont(path)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _clip_font(path):
    if not path:
        return None
    font = _font_cache.get(path)
    if font is None:
        try:
            from utilities.textclip import ClipFont
            font = ClipFont(path)
        except Exception as e:
            log.debug(f"[mirror] could not load {path}: {e}")
            font = False
        _font_cache[path] = font
    return font or None


def _mirror_text(canvas, font_proxy, x, y, color, text, kerning=0):
    cf = _clip_font(getattr(font_proxy, "_path", None))
    if cf is None:
        return
    r, g, b = color.red, color.green, color.blue
    cx = x
    for ch in text:
        glyph = cf.glyphs.get(ord(ch)) or cf.glyphs.get(0xFFFD)
        if not glyph:
            continue
        dwidth, height, y_offset, pixels, _ = glyph
        top = y - height - y_offset
        for px, py in pixels:
            canvas.mirror_pixel(cx + px, top + py, r, g, b)
        cx += dwidth + kerning


def _mirror_line(canvas, x0, y0, x1, y1, color):
    r, g, b = color.red, color.green, color.blue
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        canvas.mirror_pixel(x0, y0, r, g, b)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _mirror_circle(canvas, cx, cy, radius, color):
    r, g, b = color.red, color.green, color.blue
    x, y, err = radius, 0, 0
    while x >= y:
        for px, py in ((x, y), (y, x), (-y, x), (-x, y), (-x, -y), (-y, -x), (y, -x), (x, -y)):
            canvas.mirror_pixel(cx + px, cy + py, r, g, b)
        y += 1
        err += 1 + 2 * y
        if 2 * (err - x) + 1 > 0:
            x -= 1
            err += 1 - 2 * x


def install():
    """Wrap rgbmatrix.graphics so drawing on a MirrorCanvas is mirrored. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        from rgbmatrix import graphics
    except Exception as e:
        log.debug(f"[mirror] rgbmatrix.graphics unavailable: {e}")
        return

    real_font_cls = graphics.Font
    real_text, real_line = graphics.DrawText, graphics.DrawLine
    real_circle = getattr(graphics, "DrawCircle", None)

    def Font():
        return _FontProxy(real_font_cls)

    def DrawText(canvas, font, x, y, color, text, *args):
        real_canvas = canvas.real if isinstance(canvas, MirrorCanvas) else canvas
        real_font = font._real if isinstance(font, _FontProxy) else font
        result = real_text(real_canvas, real_font, x, y, color, text, *args)
        if isinstance(canvas, MirrorCanvas):
            try:
                _mirror_text(canvas, font, x, y, color, text, args[0] if args else 0)
            except Exception:
                pass
        return result

    def DrawLine(canvas, x0, y0, x1, y1, color):
        real_canvas = canvas.real if isinstance(canvas, MirrorCanvas) else canvas
        result = real_line(real_canvas, x0, y0, x1, y1, color)
        if isinstance(canvas, MirrorCanvas):
            try:
                _mirror_line(canvas, int(x0), int(y0), int(x1), int(y1), color)
            except Exception:
                pass
        return result

    graphics.Font = Font
    graphics.DrawText = DrawText
    graphics.DrawLine = DrawLine

    if real_circle is not None:
        def DrawCircle(canvas, x, y, radius, color):
            real_canvas = canvas.real if isinstance(canvas, MirrorCanvas) else canvas
            result = real_circle(real_canvas, x, y, radius, color)
            if isinstance(canvas, MirrorCanvas):
                try:
                    _mirror_circle(canvas, int(x), int(y), int(radius), color)
                except Exception:
                    pass
            return result
        graphics.DrawCircle = DrawCircle
    log.debug("[mirror] rgbmatrix.graphics wrapped for live preview")
