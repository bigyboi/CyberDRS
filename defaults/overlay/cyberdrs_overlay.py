#!/usr/bin/env python3
"""CyberDRS overlay renderer.

A dependency-free X11 HUD. It creates an override-redirect window tagged with
GAMESCOPE_EXTERNAL_OVERLAY so gamescope composites it on top of the running
game (the same mechanism MangoHud and the OverLaid plugin use).

The backend owns all of the logic; this process only reads
~/.cache/cyberdrs/display.json and draws it. If that file stops being
refreshed (backend died, game exited) the process exits on its own.
"""

import ctypes
import ctypes.util
import json
import os
import signal
import struct
import sys
import time

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "cyberdrs")
DISPLAY_JSON = os.path.join(CACHE_DIR, "display.json")
OUTPUT_JSON = os.path.join(CACHE_DIR, "output.json")
PID_FILE = os.path.join(CACHE_DIR, "overlay.pid")
STALE_SECONDS = 12.0

# ---------------------------------------------------------------------------
# 3x5 bitmap font (row-major, 5 rows of 3 columns). Small, but perfectly
# legible for digits and short uppercase labels once scaled up.
# ---------------------------------------------------------------------------

FONT = {
    " ": ("000", "000", "000", "000", "000"),
    "A": ("111", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "101", "101", "101"),
    "N": ("110", "101", "101", "101", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("111", "101", "111", "110", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    ":": ("000", "010", "000", "010", "000"),
    ";": ("000", "010", "000", "010", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "\\": ("100", "100", "010", "001", "001"),
    "%": ("101", "001", "010", "100", "101"),
    "(": ("010", "100", "100", "100", "010"),
    ")": ("010", "001", "001", "001", "010"),
    "[": ("110", "100", "100", "100", "110"),
    "]": ("011", "001", "001", "001", "011"),
    "?": ("111", "001", "011", "000", "010"),
    "!": ("010", "010", "010", "000", "010"),
    "_": ("000", "000", "000", "000", "111"),
    "|": ("010", "010", "010", "010", "010"),
    "=": ("000", "111", "000", "111", "000"),
    "<": ("001", "010", "100", "010", "001"),
    ">": ("100", "010", "001", "010", "100"),
    "*": ("101", "010", "101", "000", "000"),
    "'": ("010", "010", "000", "000", "000"),
    "#": ("101", "111", "101", "111", "101"),
}

# The font is single-case; let lowercase text reuse the uppercase glyphs.
for _ch in "abcdefghijklmnopqrstuvwxyz":
    FONT[_ch] = FONT[_ch.upper()]

GLYPH_W = 3
GLYPH_H = 5


def text_width(text, scale):
    if not text:
        return 0
    return len(text) * (GLYPH_W + 1) * scale - scale


def parse_color(value, fallback=0xFFFFFF):
    try:
        value = str(value).strip().lstrip("#")
        if len(value) == 3:
            value = "".join(ch * 2 for ch in value)
        if len(value) != 6:
            return fallback
        return int(value, 16)
    except Exception:
        return fallback


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    ra, ga, ba = (a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF
    rb, gb, bb = (b >> 16) & 0xFF, (b >> 8) & 0xFF, b & 0xFF
    return (
        (int(ra + (rb - ra) * t) << 16)
        | (int(ga + (gb - ga) * t) << 8)
        | int(ba + (bb - ba) * t)
    )


# ---------------------------------------------------------------------------
# Xlib bindings
# ---------------------------------------------------------------------------

c_void_p = ctypes.c_void_p
c_int = ctypes.c_int
c_uint = ctypes.c_uint
c_ulong = ctypes.c_ulong
c_char_p = ctypes.c_char_p


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", c_ulong),
        ("background_pixel", c_ulong),
        ("border_pixmap", c_ulong),
        ("border_pixel", c_ulong),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", c_int),
        ("colormap", c_ulong),
        ("cursor", c_ulong),
    ]


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", c_int),
        ("height", c_int),
        ("xoffset", c_int),
        ("format", c_int),
        ("data", c_void_p),
        ("byte_order", c_int),
        ("bitmap_unit", c_int),
        ("bitmap_bit_order", c_int),
        ("bitmap_pad", c_int),
        ("depth", c_int),
        ("bytes_per_line", c_int),
        ("bits_per_pixel", c_int),
        ("red_mask", c_ulong),
        ("green_mask", c_ulong),
        ("blue_mask", c_ulong),
        ("obdata", c_void_p),
        ("f", c_void_p * 6),
    ]


class XRectangle(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short), ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class Xlib:
    def __init__(self):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        lib = ctypes.CDLL(name)
        self.lib = lib

        def fn(fname, restype, argtypes):
            f = getattr(lib, fname)
            f.restype = restype
            f.argtypes = argtypes
            return f

        self.XOpenDisplay = fn("XOpenDisplay", c_void_p, [c_char_p])
        self.XCloseDisplay = fn("XCloseDisplay", c_int, [c_void_p])
        self.XDefaultScreen = fn("XDefaultScreen", c_int, [c_void_p])
        self.XDefaultRootWindow = fn("XDefaultRootWindow", c_ulong, [c_void_p])
        self.XDefaultVisual = fn("XDefaultVisual", c_void_p, [c_void_p, c_int])
        self.XDefaultDepth = fn("XDefaultDepth", c_int, [c_void_p, c_int])
        self.XDefaultGC = fn("XDefaultGC", c_void_p, [c_void_p, c_int])
        self.XDisplayWidth = fn("XDisplayWidth", c_int, [c_void_p, c_int])
        self.XDisplayHeight = fn("XDisplayHeight", c_int, [c_void_p, c_int])
        self.XCreateWindow = fn(
            "XCreateWindow",
            c_ulong,
            [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint, c_uint, c_int, c_uint, c_void_p, c_ulong, ctypes.POINTER(XSetWindowAttributes)],
        )
        self.XMoveResizeWindow = fn("XMoveResizeWindow", c_int, [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint])
        self.XMapRaised = fn("XMapRaised", c_int, [c_void_p, c_ulong])
        self.XStoreName = fn("XStoreName", c_int, [c_void_p, c_ulong, c_char_p])
        self.XInternAtom = fn("XInternAtom", c_ulong, [c_void_p, c_char_p, c_int])
        self.XChangeProperty = fn("XChangeProperty", c_int, [c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int, c_void_p, c_int])
        self.XCreateImage = fn(
            "XCreateImage",
            c_void_p,
            [c_void_p, c_void_p, c_uint, c_int, c_int, c_void_p, c_uint, c_uint, c_int, c_int],
        )
        self.XPutImage = fn(
            "XPutImage", c_int, [c_void_p, c_ulong, c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint, c_uint]
        )
        self.XDestroyImage = fn("XDestroyImage", c_int, [c_void_p])
        self.XFlush = fn("XFlush", c_int, [c_void_p])
        self.XSync = fn("XSync", c_int, [c_void_p, c_int])

        self.error_handler_type = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
        self.XSetErrorHandler = fn("XSetErrorHandler", c_void_p, [self.error_handler_type])

        # Optional SHAPE support for click-through.
        self.XShapeQueryExtension = None
        self.XShapeCombineRectangles = None
        try:
            xext_name = ctypes.util.find_library("Xext") or "libXext.so.6"
            xext = ctypes.CDLL(xext_name)
            self.XShapeQueryExtension = xext.XShapeQueryExtension
            self.XShapeQueryExtension.restype = c_int
            self.XShapeQueryExtension.argtypes = [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
            self.XShapeCombineRectangles = xext.XShapeCombineRectangles
            self.XShapeCombineRectangles.restype = c_int
            self.XShapeCombineRectangles.argtypes = [
                c_void_p,
                c_ulong,
                c_int,
                c_int,
                c_int,
                ctypes.POINTER(XRectangle),
                c_int,
                c_int,
                c_int,
            ]
        except Exception:
            pass


class Canvas:
    """Thin wrapper over an XImage's pixel buffer."""

    def __init__(self, image_ptr):
        self.img = XImage.from_address(image_ptr)
        self.width = self.img.width
        self.height = self.img.height
        self.stride_bytes = self.img.bytes_per_line
        self.bits = self.img.bits_per_pixel
        self.depth = self.img.depth
        self.swap = self.img.byte_order == 1  # MSBFirst
        if self.bits == 32:
            self.fmt = "<I"
            self.pxsize = 4
        elif self.bits == 16:
            self.fmt = "<H"
            self.pxsize = 2
        else:
            raise RuntimeError("unsupported XImage bits_per_pixel=%d" % self.bits)
        self.base = ctypes.cast(self.img.data, ctypes.c_void_p).value

    def _pack(self, rgb):
        if self.bits == 16:
            r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            return int.from_bytes(struct.pack("<H", value), "big") if self.swap else value
        if self.depth == 32:
            rgb |= 0xFF000000
        return int.from_bytes(struct.pack("<I", rgb), "big") if self.swap else rgb

    def fill(self, rgb):
        packed = self._pack(rgb)
        row = struct.pack(self.fmt, packed) * self.width
        for y in range(self.height):
            ctypes.memmove(self.base + y * self.stride_bytes, row, self.width * self.pxsize)

    def rect(self, x, y, w, h, rgb):
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        packed = self._pack(rgb)
        row = struct.pack(self.fmt, packed) * (x1 - x0)
        span = (x1 - x0) * self.pxsize
        for yy in range(y0, y1):
            ctypes.memmove(self.base + yy * self.stride_bytes + x0 * self.pxsize, row, span)

    def text(self, x, y, text, scale, rgb):
        cx = x
        for ch in text:
            glyph = FONT.get(ch) or FONT.get(ch.upper()) or FONT["?"]
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(cx + col * scale, y + row * scale, scale, scale, rgb)
            cx += (GLYPH_W + 1) * scale


def load_display():
    try:
        with open(DISPLAY_JSON, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def style_of(data):
    raw = data.get("style") if isinstance(data.get("style"), dict) else {}
    scale = max(1, min(12, int(raw.get("scale", 4))))
    return {
        "scale": scale,
        "opacity": max(0, min(100, int(raw.get("opacity", 88)))),
        "margin": max(0, min(200, int(raw.get("margin", 18)))),
        "position": raw.get("position", "top-right"),
        "fg": parse_color(raw.get("fg"), 0x00E5FF),
        "accent": parse_color(raw.get("accent"), 0xFFD400),
        "dim": parse_color(raw.get("dim"), 0x9AA0B4),
        "warn": parse_color(raw.get("warn"), 0xFF6B6B),
        "bg": parse_color(raw.get("bg"), 0x08080C),
        "border": bool(raw.get("border", True)),
    }


def color_for(style, key):
    if key == "accent":
        return style["accent"]
    if key == "dim":
        return style["dim"]
    if key == "warn":
        return style["warn"]
    return style["fg"]


def layout(data):
    style = style_of(data)
    scale = style["scale"]
    title = str(data.get("title") or "")
    lines = data.get("lines")
    if not isinstance(lines, list):
        lines = []
    lines = [ln for ln in lines if isinstance(ln, dict) and ln.get("t")]

    pad = max(4, 3 * scale)
    border = max(1, scale // 2) if style["border"] else 0
    gap = max(3, 2 * scale)
    line_h = GLYPH_H * scale
    widths = [text_width(title, scale)] if title else []
    widths += [text_width(str(ln.get("t")), scale) for ln in lines]
    text_w = max(widths) if widths else text_width("WAIT", scale)
    blocks = (1 if title else 0) + len(lines)
    height = pad * 2 + blocks * line_h + max(0, blocks - 1) * gap
    width = pad * 2 + text_w
    return style, title, lines, width + border * 2, height + border * 2, pad, border, gap, line_h


def draw(canvas, data):
    style, title, lines, width, height, pad, border, gap, line_h = layout(data)
    canvas.fill(style["bg"])

    if border:
        edge = mix(style["accent"], style["bg"], 0.45)
        canvas.rect(0, 0, width, border, edge)
        canvas.rect(0, height - border, width, border, edge)
        canvas.rect(0, 0, border, height, edge)
        canvas.rect(width - border, 0, border, height, edge)

    scale = style["scale"]
    y = pad + border
    if title:
        canvas.text(pad + border, y, title, scale, style["accent"])
        y += line_h + gap
    for line in lines:
        canvas.text(pad + border, y, str(line.get("t")), scale, color_for(style, line.get("c", "fg")))
        y += line_h + gap


def place(style, screen_w, screen_h, width, height):
    margin = style["margin"]
    pos = style["position"]
    if pos == "top-left":
        x, y = margin, margin
    elif pos == "bottom-left":
        x, y = margin, screen_h - height - margin
    elif pos == "bottom-right":
        x, y = screen_w - width - margin, screen_h - height - margin
    else:
        x, y = screen_w - width - margin, margin
    return max(0, x), max(0, y)


def write_output(width, height):
    tmp = OUTPUT_JSON + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"w": width, "h": height, "ts": time.time()}, fh)
        os.replace(tmp, OUTPUT_JSON)
    except Exception:
        pass


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass

    x = Xlib()

    errors = []

    def on_error(display, event):
        errors.append(1)
        return 0

    handler = x.error_handler_type(on_error)
    x.XSetErrorHandler(handler)

    # If the game's own DISPLAY doesn't work, fall back to the usual
    # gamescope display numbers instead of dying silently.
    tried = []
    names = []
    primary = os.environ.get("DISPLAY")
    if primary:
        names.append(primary.encode())
    for alt in (b":0", b":1", b":2"):
        if alt not in names:
            names.append(alt)

    dpy = None
    for name in names:
        dpy = x.XOpenDisplay(name)
        if dpy:
            display_name = name
            break
        tried.append(name.decode())
        print(
            "CyberDRS overlay: cannot open display %r (DISPLAY=%r XAUTHORITY=%r)"
            % (name.decode(), os.environ.get("DISPLAY"), os.environ.get("XAUTHORITY")),
            flush=True,
        )
    if not dpy:
        print(
            "CyberDRS overlay: no usable X display, tried: %s" % (", ".join(tried)),
            flush=True,
        )
        time.sleep(5)
        return 1

    screen = x.XDefaultScreen(dpy)
    root = x.XDefaultRootWindow(dpy)
    visual = x.XDefaultVisual(dpy, screen)
    depth = x.XDefaultDepth(dpy, screen)
    gc = x.XDefaultGC(dpy, screen)
    screen_w = x.XDisplayWidth(dpy, screen)
    screen_h = x.XDisplayHeight(dpy, screen)

    attrs = XSetWindowAttributes()
    attrs.background_pixel = 0
    attrs.override_redirect = 1
    CWBackPixel = 1 << 1
    CWOverrideRedirect = 1 << 9
    win = x.XCreateWindow(
        dpy,
        root,
        0,
        0,
        1,
        1,
        0,
        depth,
        1,  # InputOutput
        visual,
        CWBackPixel | CWOverrideRedirect,
        ctypes.byref(attrs),
    )
    if not win:
        print("CyberDRS overlay: XCreateWindow failed", flush=True)
        x.XCloseDisplay(dpy)
        return 1

    x.XStoreName(dpy, win, b"CyberDRS")

    def set_cardinal(name, value):
        atom = x.XInternAtom(dpy, name.encode(), False)
        val = c_ulong(int(value) & 0xFFFFFFFF)
        x.XChangeProperty(dpy, win, atom, 6, 32, 0, ctypes.cast(ctypes.byref(val), c_void_p), 1)

    # Tell gamescope this is an external overlay, and set its alpha.
    set_cardinal("GAMESCOPE_EXTERNAL_OVERLAY", 1)

    def set_opacity(percent):
        set_cardinal("_NET_WM_WINDOW_OPACITY", int(round(percent / 100.0 * 0xFFFFFFFF)))

    set_opacity(88)

    # Click-through (best effort: an empty input shape).
    try:
        if x.XShapeQueryExtension:
            ev_base = c_int()
            err_base = c_int()
            if x.XShapeQueryExtension(dpy, ctypes.byref(ev_base), ctypes.byref(err_base)):
                x.XShapeCombineRectangles(dpy, win, 2, 0, 0, None, 0, 0, 0)
    except Exception:
        pass

    x.XMapRaised(dpy, win)
    x.XFlush(dpy)

    print("CyberDRS overlay: window 0x%x on %s (%dx%d)" % (win, display_name.decode(), screen_w, screen_h), flush=True)
    write_output(screen_w, screen_h)

    last_mtime = None
    last_opacity = None
    current_size = (0, 0)
    current_pos = None
    stale_since = time.time()
    ticks = 0
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        try:
            mtime = os.path.getmtime(DISPLAY_JSON)
        except OSError:
            mtime = None

        if mtime is not None and mtime != last_mtime:
            last_mtime = mtime
            stale_since = time.time()
            data = load_display()
            if data:
                style, _title, _lines, width, height, *_ = layout(data)
                if style["opacity"] != last_opacity:
                    set_opacity(style["opacity"])
                    last_opacity = style["opacity"]
                if (width, height) != current_size:
                    current_size = (width, height)
                    current_pos = None
                pos = place(style, screen_w, screen_h, width, height)
                if pos != current_pos:
                    x.XMoveResizeWindow(dpy, win, pos[0], pos[1], width, height)
                    current_pos = pos
                image = x.XCreateImage(dpy, visual, depth, 2, 0, None, width, height, 32, 0)
                if image:
                    try:
                        canvas = Canvas(image)
                        draw(canvas, data)
                        x.XPutImage(dpy, win, gc, image, 0, 0, 0, 0, width, height)
                    except Exception as exc:
                        print("CyberDRS overlay: draw failed: %s" % exc, flush=True)
                    x.XDestroyImage(image)
                x.XFlush(dpy)

        ticks += 1
        if ticks % 8 == 0:
            sw = x.XDisplayWidth(dpy, screen)
            sh = x.XDisplayHeight(dpy, screen)
            if (sw, sh) != (screen_w, screen_h):
                screen_w, screen_h = sw, sh
                current_pos = None
                write_output(screen_w, screen_h)

        if time.time() - stale_since > STALE_SECONDS:
            print("CyberDRS overlay: backend data went stale, exiting", flush=True)
            break

        time.sleep(0.25)

    try:
        x.XCloseDisplay(dpy)
    except Exception:
        pass
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except SystemExit:
        raise
    except BaseException:
        # Crash on device: leave a readable traceback in overlay.log instead
        # of dying silently and being respawned forever.
        import traceback

        traceback.print_exc()
        _rc = 1
        time.sleep(2)
    sys.exit(_rc)
