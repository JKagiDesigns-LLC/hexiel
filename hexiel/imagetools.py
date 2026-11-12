"""The `image` tool: look at, create and change images in Python (Pillow), so any model — text-only, small, local —
can do image work without vision and without writing a script. Every op returns a short text summary plus the
path it wrote; the TUI and web UI show the file right under the tool line (ToolResult.outputs).

ops
  info     size, format, mode, DPI, alpha, EXIF (camera, date, GPS present), colours, layout, OCR text (the image card)
  edit     apply `steps` in order to `path` → `out`
  create   a new canvas (width, height, background colour or gradient), then `steps` → `out`
  combine  several images into a grid / row / column / contact sheet, with optional labels → `out`
  diff     compare `path` with `other`: changed share, changed regions boxed on a copy → `out`
  convert  one file or a glob/list to another format, optionally compressed to `max_kb`, metadata stripped
  gif      frames (list or glob) → animated GIF
  qr       a QR code for `text` (segno when installed, else qrcode)
  icons    favicon.ico + web/iOS/Android icon sizes from one image into a folder

edit/create steps (a list of objects, each with "do"):
  crop {box:[x,y,w,h]} | {trim:true} | {aspect:"16:9"}      resize {width?,height?,fit:"contain|cover|stretch"} | {scale:0.5}
  rotate {degrees, expand?}     flip {direction:"horizontal|vertical"}     pad {width,height,color?}
  adjust {brightness?,contrast?,saturation?,sharpness?,gamma?} (1.0 = unchanged)
  filter {name:"blur|sharpen|smooth|detail|edges|emboss|contour", radius?}     grayscale | sepia | invert
  replace_color {from, to, tolerance?}     transparent {color, tolerance?}     opacity {value 0..1}
  round_corners {radius}     border {width, color}     shadow {offset?, blur?, color?}
  text {text, at:"x,y"|"center|top|bottom|top-left|…", size?, color?, stroke?, stroke_color?, background?, font?}
  shape {kind:"rect|ellipse|line|arrow", box:[x,y,w,h] | points:[[x,y],…], color?, fill?, width?}
  watermark {text | image, at?, opacity?, size?}     redact {box:[x,y,w,h], mode:"black|blur|pixelate"}
  paste {image, at:"x,y"|position, width?}     gradient {from, to, direction:"vertical|horizontal"} (fills the canvas)
"""

from __future__ import annotations

import glob as globmod
import math
import os
import shutil
from pathlib import Path

POSITIONS = ("top-left", "top", "top-right", "left", "center", "centre", "right",
             "bottom-left", "bottom", "bottom-right")
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF", ".bmp": "BMP",
           ".tif": "TIFF", ".tiff": "TIFF", ".ico": "ICO", ".pdf": "PDF", ".avif": "AVIF"}
ICON_SETS = {
    "web": [16, 32, 48, 64, 128, 180, 192, 256, 512],
    "ios": [20, 29, 40, 58, 60, 76, 80, 87, 120, 152, 167, 180, 1024],
    "android": [36, 48, 72, 96, 144, 192, 512],
}


class ImageError(ValueError):
    """A problem the model can fix (bad argument, missing file): said as one line, never a traceback."""


# ---------------------------------------------------------------- small helpers

def _pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - Pillow is a dependency
        raise ImageError("Pillow is not installed: pip install pillow") from exc
    from PIL import Image
    return Image


def _resolve(path, cwd: str) -> Path:
    if not path:
        raise ImageError("a path is needed")
    p = Path(os.path.expanduser(str(path)))
    return p if p.is_absolute() else Path(cwd) / p


def _open(path: Path):
    Image = _pil()
    if path.suffix.lower() in (".heic", ".heif"):
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            raise ImageError("reading HEIC photos needs pillow-heif: pip install pillow-heif "
                             "(or pip install 'hexiel[images]')") from None
    if not path.is_file():
        raise ImageError(f"no such image: {path}")
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"can't read {path.name} as an image ({exc})") from None
    return img


def _color(value, default="black"):
    from PIL import ImageColor
    value = default if value in (None, "") else value
    if isinstance(value, (list, tuple)):
        return tuple(int(v) for v in value)
    try:
        return ImageColor.getrgb(str(value))
    except ValueError:
        raise ImageError(f"unknown colour {value!r} — use a name (red), #rrggbb or rgb(…)") from None


def _font(size: int = 32, name: str | None = None):
    from PIL import ImageFont
    candidates = [name] if name else []
    candidates += ["DejaVuSans.ttf", "NotoSans-Regular.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttc",
                   "/usr/share/fonts/noto/NotoSans-Regular.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc",
                   "C:/Windows/Fonts/arial.ttf"]
    for c in candidates:
        if not c:
            continue
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    match = shutil.which("fc-match")
    if match:
        import subprocess
        try:
            found = subprocess.run([match, "-f", "%{file}", name or "sans"], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
            if found:
                return ImageFont.truetype(found, size)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def _box(value, img) -> tuple[int, int, int, int]:
    """[x, y, w, h] (numbers or percentages like "10%") → (left, top, right, bottom), clipped to the image."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ImageError("box must be [x, y, width, height]")
    W, H = img.size

    def num(v, total):
        s = str(v).strip()
        return int(float(s[:-1]) * total / 100) if s.endswith("%") else int(float(s))
    x, y, w, h = num(value[0], W), num(value[1], H), num(value[2], W), num(value[3], H)
    left, top = max(0, x), max(0, y)
    right, bottom = min(W, x + w), min(H, y + h)
    if right <= left or bottom <= top:
        raise ImageError(f"box {value} is outside the {W}×{H} image")
    return left, top, right, bottom


def _place(at, size: tuple[int, int], canvas: tuple[int, int], margin: int | None = None) -> tuple[int, int]:
    """Where a w×h thing goes on the canvas: "x,y" / [x, y] or a position word."""
    W, H = canvas
    w, h = size
    m = int(min(W, H) * 0.03) if margin is None else margin
    if isinstance(at, (list, tuple)) and len(at) == 2:
        return int(at[0]), int(at[1])
    s = str(at or "center").strip().lower()
    if "," in s:
        a, b = s.split(",", 1)
        return int(float(a)), int(float(b))
    if s not in POSITIONS:
        raise ImageError(f"at must be 'x,y' or one of {', '.join(POSITIONS)}")
    x = m if "left" in s else W - w - m if "right" in s else (W - w) // 2
    y = m if s.startswith("top") else H - h - m if s.startswith("bottom") else (H - h) // 2
    return x, y


def _rgba(img):
    return img if img.mode == "RGBA" else img.convert("RGBA")


def _summary(path: Path, img=None) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    dims = f"{img.size[0]}×{img.size[1]} " if img is not None else ""
    return f"{path} ({dims}{_human(size)})"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _save(img, out: Path, quality: int | None = None, strip: bool = False, keep_exif=None) -> Path:
    fmt = FORMATS.get(out.suffix.lower())
    if fmt is None:
        raise ImageError(f"unknown output type {out.suffix or '(none)'} — use one of {', '.join(sorted(FORMATS))}")
    out.parent.mkdir(parents=True, exist_ok=True)
    kw = {}
    if fmt in ("JPEG", "WEBP", "AVIF"):
        kw["quality"] = int(quality or 88)
    if fmt == "JPEG":
        kw["optimize"] = True
        if img.mode not in ("RGB", "L"):
            bg = _pil().new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
    if fmt == "PNG":
        kw["optimize"] = True
    if fmt in ("PDF", "BMP") and img.mode == "RGBA":
        img = img.convert("RGB")
    if fmt == "GIF" and img.mode not in ("P", "L"):
        img = img.convert("P", palette=_pil().Palette.ADAPTIVE)
    if keep_exif and not strip and fmt in ("JPEG", "WEBP", "PNG"):
        kw["exif"] = keep_exif
    img.save(out, fmt, **kw)
    return out


def _default_out(path: Path | None, cwd: str, suffix: str, ext: str | None = None) -> Path:
    if path is None:
        return Path(cwd) / f"image{suffix}{ext or '.png'}"
    return path.with_name(f"{path.stem}{suffix}{ext or path.suffix}")


# ---------------------------------------------------------------- steps

def _apply(img, step: dict, cwd: str):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
    if isinstance(step, str):
        step = {"do": step}
    if not isinstance(step, dict):
        raise ImageError("each step is an object like {\"do\": \"resize\", \"width\": 800}")
    do = str(step.get("do") or step.get("op") or "").strip().lower()
    W, H = img.size

    if do == "crop":
        if step.get("trim"):
            rgb = img.convert("RGB")
            bg = Image.new("RGB", img.size, rgb.getpixel((0, 0)))
            from PIL import ImageChops
            bbox = ImageChops.difference(rgb, bg).point(lambda v: 255 if v > 12 else 0).getbbox()
            return img.crop(bbox) if bbox else img
        if step.get("aspect"):
            a, b = (float(v) for v in str(step["aspect"]).replace("/", ":").split(":"))
            target = a / b
            if W / H > target:
                nw = int(H * target)
                return img.crop(((W - nw) // 2, 0, (W - nw) // 2 + nw, H))
            nh = int(W / target)
            return img.crop((0, (H - nh) // 2, W, (H - nh) // 2 + nh))
        return img.crop(_box(step.get("box"), img))
    if do == "resize":
        if step.get("scale"):
            s = float(step["scale"])
            return img.resize((max(1, int(W * s)), max(1, int(H * s))), Image.LANCZOS)
        w, h = step.get("width"), step.get("height")
        if not w and not h:
            raise ImageError("resize needs width and/or height, or scale")
        fit = str(step.get("fit", "contain")).lower()
        if w and h and fit == "cover":
            return ImageOps.fit(img, (int(w), int(h)), Image.LANCZOS)
        if w and h and fit == "stretch":
            return img.resize((int(w), int(h)), Image.LANCZOS)
        if w and h:
            out = img.copy()
            out.thumbnail((int(w), int(h)), Image.LANCZOS) if (W > int(w) or H > int(h)) else None
            if out.size == img.size and (W < int(w) and H < int(h)):
                s = min(int(w) / W, int(h) / H)
                out = img.resize((int(W * s), int(H * s)), Image.LANCZOS)
            return out
        s = int(w) / W if w else int(h) / H
        return img.resize((max(1, int(W * s)), max(1, int(H * s))), Image.LANCZOS)
    if do == "rotate":
        return _rgba(img).rotate(-float(step.get("degrees", 90)), expand=bool(step.get("expand", True)),
                                 resample=Image.BICUBIC)
    if do == "flip":
        d = str(step.get("direction", "horizontal")).lower()
        return ImageOps.mirror(img) if d.startswith("h") else ImageOps.flip(img)
    if do == "pad":
        w, h = int(step.get("width", W)), int(step.get("height", H))
        canvas = Image.new("RGBA", (max(w, W), max(h, H)), _color(step.get("color"), "white") + (255,))
        canvas.paste(_rgba(img), ((canvas.width - W) // 2, (canvas.height - H) // 2), _rgba(img))
        return canvas
    if do == "adjust":
        out = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img
        alpha = out.split()[-1] if out.mode == "RGBA" else None
        rgb = out.convert("RGB")
        for key, enh in (("brightness", ImageEnhance.Brightness), ("contrast", ImageEnhance.Contrast),
                         ("saturation", ImageEnhance.Color), ("sharpness", ImageEnhance.Sharpness)):
            if step.get(key) is not None:
                rgb = enh(rgb).enhance(float(step[key]))
        if step.get("gamma"):
            g = float(step["gamma"])
            rgb = rgb.point(lambda v: int(255 * ((v / 255) ** (1 / g))))
        if alpha is not None:
            rgb = rgb.convert("RGBA")
            rgb.putalpha(alpha)
        return rgb
    if do == "filter":
        name = str(step.get("name", "blur")).lower()
        r = float(step.get("radius", 2))
        f = {"blur": ImageFilter.GaussianBlur(r), "sharpen": ImageFilter.UnsharpMask(radius=r, percent=150),
             "smooth": ImageFilter.SMOOTH_MORE, "detail": ImageFilter.DETAIL, "edges": ImageFilter.FIND_EDGES,
             "emboss": ImageFilter.EMBOSS, "contour": ImageFilter.CONTOUR}.get(name)
        if f is None:
            raise ImageError("filter name: blur, sharpen, smooth, detail, edges, emboss or contour")
        base = img.convert("RGBA") if img.mode == "P" else img
        return base.filter(f)
    if do in ("grayscale", "greyscale"):
        return ImageOps.grayscale(img).convert("RGBA" if img.mode == "RGBA" else "RGB")
    if do == "sepia":
        grey = ImageOps.grayscale(img)
        return ImageOps.colorize(grey, (40, 26, 13), (255, 240, 192))
    if do == "invert":
        if img.mode == "RGBA":
            r, g, b, a = img.split()
            inv = ImageOps.invert(Image.merge("RGB", (r, g, b))).convert("RGBA")
            inv.putalpha(a)
            return inv
        return ImageOps.invert(img.convert("RGB"))
    if do in ("replace_color", "transparent"):
        src = _color(step.get("from") if do == "replace_color" else step.get("color"), "white")
        tol = int(step.get("tolerance", 30))
        dst = (_color(step.get("to"), "black") + (255,)) if do == "replace_color" else (0, 0, 0, 0)
        out = _rgba(img).copy()
        px = out.load()
        for yy in range(out.height):
            for xx in range(out.width):
                p = px[xx, yy]
                if abs(p[0] - src[0]) <= tol and abs(p[1] - src[1]) <= tol and abs(p[2] - src[2]) <= tol:
                    px[xx, yy] = dst
        return out
    if do == "opacity":
        out = _rgba(img).copy()
        a = out.split()[-1].point(lambda v: int(v * float(step.get("value", 0.5))))
        out.putalpha(a)
        return out
    if do == "round_corners":
        r = int(step.get("radius", min(W, H) // 10))
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), r, fill=255)
        out = _rgba(img).copy()
        out.putalpha(Image.composite(out.split()[-1], Image.new("L", img.size, 0), mask))
        return out
    if do == "border":
        return ImageOps.expand(_rgba(img), border=int(step.get("width", 10)),
                               fill=_color(step.get("color"), "black") + (255,))
    if do == "shadow":
        off = int(step.get("offset", max(4, min(W, H) // 40)))
        blur = int(step.get("blur", off * 2))
        pad = blur * 2 + off
        canvas = Image.new("RGBA", (W + pad * 2, H + pad * 2), (0, 0, 0, 0))
        shade = Image.new("RGBA", img.size, _color(step.get("color"), "black") + (140,))
        canvas.paste(shade, (pad + off, pad + off), _rgba(img).split()[-1])
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
        canvas.paste(_rgba(img), (pad, pad), _rgba(img))
        return canvas
    if do == "gradient":
        a, b = _color(step.get("from"), "#1e3a8a"), _color(step.get("to"), "#9333ea")
        vertical = not str(step.get("direction", "vertical")).lower().startswith("h")
        n = H if vertical else W
        line = Image.new("RGB", (1, n) if vertical else (n, 1))
        for i in range(n):
            t = i / max(1, n - 1)
            c = tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))
            line.putpixel((0, i) if vertical else (i, 0), c)
        grad = line.resize((W, H)).convert("RGBA")
        base = _rgba(img).copy()
        base.alpha_composite(grad)
        return base
    if do == "text":
        out = _rgba(img).copy()
        draw = ImageDraw.Draw(out)
        text = str(step.get("text", ""))
        font = _font(int(step.get("size", max(14, H // 12))), step.get("font"))
        stroke = int(step.get("stroke", 0))
        bbox = draw.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, align=step.get("align", "center"))
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = _place(step.get("at", "center"), (tw, th), out.size)
        if step.get("background"):
            p = max(6, th // 4)
            layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle((x - p, y - p, x + tw + p, y + th + p), p,
                                                    fill=_color(step["background"]) + (int(255 * float(step.get("background_opacity", 0.75))),))
            out.alpha_composite(layer)
            draw = ImageDraw.Draw(out)
        draw.multiline_text((x - bbox[0], y - bbox[1]), text, font=font, fill=_color(step.get("color"), "white"),
                            stroke_width=stroke, stroke_fill=_color(step.get("stroke_color"), "black"),
                            align=step.get("align", "center"))
        return out
    if do == "shape":
        out = _rgba(img).copy()
        draw = ImageDraw.Draw(out)
        kind = str(step.get("kind", "rect")).lower()
        color = _color(step.get("color"), "red")
        width = int(step.get("width", max(2, min(W, H) // 150)))
        fill = _color(step["fill"]) if step.get("fill") else None
        if kind in ("rect", "rectangle", "ellipse", "circle"):
            l, t, r, b = _box(step.get("box"), img)
            fn = draw.rectangle if kind.startswith("rect") else draw.ellipse
            fn((l, t, r, b), outline=color, width=width, fill=fill)
        elif kind in ("line", "arrow"):
            pts = [tuple(int(v) for v in p) for p in (step.get("points") or [])]
            if len(pts) < 2:
                raise ImageError("a line needs points: [[x1, y1], [x2, y2]]")
            draw.line(pts, fill=color, width=width)
            if kind == "arrow":
                (x1, y1), (x2, y2) = pts[-2], pts[-1]
                ang = math.atan2(y2 - y1, x2 - x1)
                head = width * 5
                left = (x2 - head * math.cos(ang - 0.45), y2 - head * math.sin(ang - 0.45))
                right = (x2 - head * math.cos(ang + 0.45), y2 - head * math.sin(ang + 0.45))
                draw.polygon([(x2, y2), left, right], fill=color)
        else:
            raise ImageError("shape kind: rect, ellipse, line or arrow")
        return out
    if do == "watermark":
        out = _rgba(img).copy()
        op = float(step.get("opacity", 0.35))
        if step.get("image"):
            mark = _rgba(_open(_resolve(step["image"], cwd)))
            target = int(W * float(step.get("size", 0.2)))
            mark = mark.resize((target, max(1, int(mark.height * target / mark.width))), Image.LANCZOS)
        else:
            font = _font(int(step.get("size", max(14, H // 18))) if float(step.get("size", 0) or 0) > 1 else max(14, H // 18))
            text = str(step.get("text", "©"))
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            bb = probe.textbbox((0, 0), text, font=font)
            mark = Image.new("RGBA", (bb[2] - bb[0] + 4, bb[3] - bb[1] + 4), (0, 0, 0, 0))
            ImageDraw.Draw(mark).text((2 - bb[0], 2 - bb[1]), text, font=font, fill=_color(step.get("color"), "white"))
        mark.putalpha(mark.split()[-1].point(lambda v: int(v * op)))
        x, y = _place(step.get("at", "bottom-right"), mark.size, out.size)
        out.alpha_composite(mark, (max(0, x), max(0, y)))
        return out
    if do == "redact":
        out = _rgba(img).copy()
        l, t, r, b = _box(step.get("box"), img)
        mode = str(step.get("mode", "black")).lower()
        region = out.crop((l, t, r, b))
        if mode == "blur":
            region = region.filter(ImageFilter.GaussianBlur(max(8, (r - l) // 8)))
        elif mode == "pixelate":
            k = max(4, (r - l) // 12)
            region = region.resize((max(1, (r - l) // k), max(1, (b - t) // k)), Image.NEAREST).resize((r - l, b - t), Image.NEAREST)
        else:
            region = Image.new("RGBA", region.size, _color(step.get("color"), "black") + (255,))
        out.paste(region, (l, t))
        return out
    if do == "paste":
        out = _rgba(img).copy()
        other = _rgba(_open(_resolve(step.get("image"), cwd)))
        if step.get("width"):
            w = int(step["width"])
            other = other.resize((w, max(1, int(other.height * w / other.width))), Image.LANCZOS)
        x, y = _place(step.get("at", "center"), other.size, out.size)
        out.alpha_composite(other, (max(0, x), max(0, y)))
        return out
    raise ImageError(f"unknown step {do!r} — see the image tool's description for the list")


# ---------------------------------------------------------------- ops

def info(args: dict, cwd: str) -> tuple[str, list]:
    path = _resolve(args.get("path"), cwd)
    img = _open(path)
    lines = [f"{path.name}: {img.size[0]}×{img.size[1]} {img.format or '?'} {img.mode}, {_human(path.stat().st_size)}"]
    if getattr(img, "n_frames", 1) > 1:
        lines.append(f"frames: {img.n_frames}")
    dpi = img.info.get("dpi")
    if dpi:
        lines.append(f"dpi: {round(dpi[0])}×{round(dpi[1])}")
    lines.append("transparency: " + ("yes" if img.mode in ("RGBA", "LA") or "transparency" in img.info else "no"))
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001
        exif = {}
    if exif:
        from PIL import ExifTags
        tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
        keep = [f"{k}: {tags[k]}" for k in ("Make", "Model", "DateTime", "Software", "Orientation") if k in tags]
        try:
            if exif.get_ifd(0x8825):
                keep.append("GPS location: present (strip it before sharing: op=convert strip=true)")
        except Exception:  # noqa: BLE001
            pass
        if keep:
            lines.append("EXIF: " + "; ".join(str(k) for k in keep))
    from . import vision
    card = vision.image_card(path.read_bytes())
    lines.append(card.split("\n", 1)[1] if "\n" in card else card)
    return "\n".join(lines), []


def edit(args: dict, cwd: str) -> tuple[str, list]:
    path = _resolve(args.get("path"), cwd)
    img = _open(path)
    steps = args.get("steps") or []
    if isinstance(steps, dict):
        steps = [steps]
    if not steps:
        raise ImageError("edit needs steps, e.g. [{\"do\": \"resize\", \"width\": 800}]")
    exif = img.info.get("exif")
    for step in steps:
        img = _apply(img, step, cwd)
    out = _resolve(args["out"], cwd) if args.get("out") else _default_out(path, cwd, "-edited")
    _save(img, out, args.get("quality"), bool(args.get("strip")), exif)
    did = ", ".join(str((s.get("do") if isinstance(s, dict) else s)) for s in steps)
    return f"{did} → {_summary(out, img)}", [out]


def create(args: dict, cwd: str) -> tuple[str, list]:
    Image = _pil()
    w, h = int(args.get("width") or 1200), int(args.get("height") or 630)
    if w * h > 64_000_000:
        raise ImageError("that canvas is too large (over 64 megapixels)")
    bg = args.get("background", "white")
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0) if str(bg).lower() == "transparent" else _color(bg) + (255,))
    for step in args.get("steps") or []:
        img = _apply(img, step, cwd)
    out = _resolve(args["out"], cwd) if args.get("out") else _default_out(None, cwd, "")
    _save(img, out, args.get("quality"))
    return f"created {_summary(out, img)}", [out]


def _expand(paths, cwd: str) -> list[Path]:
    if isinstance(paths, str):
        paths = [paths]
    found: list[Path] = []
    for p in paths or []:
        full = str(_resolve(p, cwd))
        hits = sorted(globmod.glob(full)) if any(c in full for c in "*?[") else [full]
        found.extend(Path(h) for h in hits)
    return found


def combine(args: dict, cwd: str) -> tuple[str, list]:
    from PIL import Image, ImageDraw
    files = _expand(args.get("paths"), cwd)
    if len(files) < 2:
        raise ImageError("combine needs at least two images in paths (a list or a glob)")
    if len(files) > 400:
        raise ImageError(f"{len(files)} images is too many for one sheet (400 at most)")
    imgs = [_rgba(_open(f)) for f in files]
    layout = str(args.get("layout", "grid")).lower()
    gap = int(args.get("gap", 12))
    cell = int(args.get("cell", 360 if layout == "grid" else 0) or 0)
    labels = args.get("labels")
    if labels is True:
        labels = [f.name for f in files]
    if layout in ("row", "column"):
        if layout == "row":
            h = cell or min(i.height for i in imgs)
            imgs = [i.resize((max(1, int(i.width * h / i.height)), h), Image.LANCZOS) for i in imgs]
        else:
            w = cell or min(i.width for i in imgs)
            imgs = [i.resize((w, max(1, int(i.height * w / i.width))), Image.LANCZOS) for i in imgs]
        cols = len(imgs) if layout == "row" else 1
    else:
        cols = int(args.get("columns") or math.ceil(math.sqrt(len(imgs))))
        for i in imgs:
            i.thumbnail((cell, cell), Image.LANCZOS)
    rows = math.ceil(len(imgs) / cols)
    label_h = 0
    font = None
    if labels:
        font = _font(max(12, (cell or 300) // 18))
        label_h = font.size + 8 if hasattr(font, "size") else 22
    col_w = [max((imgs[r * cols + c].width for r in range(rows) if r * cols + c < len(imgs)), default=0) for c in range(cols)]
    row_h = [max((imgs[r * cols + c].height for c in range(cols) if r * cols + c < len(imgs)), default=0) + label_h for r in range(rows)]
    W = sum(col_w) + gap * (cols + 1)
    H = sum(row_h) + gap * (rows + 1)
    sheet = Image.new("RGBA", (W, H), _color(args.get("background"), "white") + (255,))
    draw = ImageDraw.Draw(sheet)
    y = gap
    for r in range(rows):
        x = gap
        for c in range(cols):
            k = r * cols + c
            if k >= len(imgs):
                break
            im = imgs[k]
            sheet.alpha_composite(im, (x + (col_w[c] - im.width) // 2, y + (row_h[r] - label_h - im.height) // 2))
            if labels and k < len(labels):
                draw.text((x + 2, y + row_h[r] - label_h + 2), str(labels[k])[:40], font=font, fill=_color(args.get("label_color"), "#333333"))
            x += col_w[c] + gap
        y += row_h[r] + gap
    out = _resolve(args["out"], cwd) if args.get("out") else Path(cwd) / "combined.png"
    _save(sheet, out, args.get("quality"))
    return f"combined {len(imgs)} images ({layout}, {cols}×{rows}) → {_summary(out, sheet)}", [out]


def diff(args: dict, cwd: str) -> tuple[str, list]:
    from PIL import Image, ImageChops, ImageDraw
    a_path, b_path = _resolve(args.get("path"), cwd), _resolve(args.get("other"), cwd)
    a, b = _open(a_path).convert("RGB"), _open(b_path).convert("RGB")
    note = ""
    if a.size != b.size:
        note = f" (sizes differ: {a.size[0]}×{a.size[1]} vs {b.size[0]}×{b.size[1]}; compared at the first's size)"
        b = b.resize(a.size, Image.LANCZOS)
    threshold = int(args.get("threshold", 24))
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
    changed = sum(1 for v in mask.getdata() if v)
    share = changed / (a.size[0] * a.size[1])
    # regions: connected areas on a coarse grid (cells of ~1% of the width), merged into boxes
    W, H = a.size
    cs = max(4, W // 100)
    gw, gh = math.ceil(W / cs), math.ceil(H / cs)
    small = mask.resize((gw, gh), Image.BOX)
    hot = {(x, y) for y in range(gh) for x in range(gw) if small.getpixel((x, y)) > 0}
    boxes = []
    while hot:
        stack = [hot.pop()]
        xs, ys = [], []
        while stack:
            x, y = stack.pop()
            xs.append(x)
            ys.append(y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in hot:
                        hot.remove(n)
                        stack.append(n)
        boxes.append((min(xs) * cs, min(ys) * cs, min(W, (max(xs) + 1) * cs), min(H, (max(ys) + 1) * cs)))
    boxes.sort(key=lambda bx: -(bx[2] - bx[0]) * (bx[3] - bx[1]))
    marked = b.convert("RGBA")
    draw = ImageDraw.Draw(marked)
    for bx in boxes[:50]:
        draw.rectangle(bx, outline=(255, 0, 64, 255), width=max(2, W // 300))
    out = _resolve(args["out"], cwd) if args.get("out") else _default_out(b_path, cwd, "-diff", ".png")
    _save(marked, out)
    if not changed:
        return f"identical{note} (threshold {threshold})", []
    lines = [f"{share:.2%} of the pixels differ{note}; {len(boxes)} changed region(s), largest first:"]
    lines += [f"  x={x0} y={y0} {x1 - x0}×{y1 - y0}" for x0, y0, x1, y1 in boxes[:12]]
    lines.append(f"marked copy → {_summary(out, marked)}")
    return "\n".join(lines), [out]


def convert(args: dict, cwd: str) -> tuple[str, list]:
    files = _expand(args.get("paths") or args.get("path"), cwd)
    if not files:
        raise ImageError("convert needs path (or paths / a glob)")
    fmt = str(args.get("format") or "").lower().lstrip(".")
    max_kb = float(args.get("max_kb") or 0)
    strip = bool(args.get("strip"))
    out_dir = _resolve(args["out"], cwd) if args.get("out") and len(files) > 1 else None
    done, lines = [], []
    for f in files:
        img = _open(f)
        exif = img.info.get("exif")
        ext = f".{fmt}" if fmt else f.suffix
        if ext.lower() == ".jpeg":
            ext = ".jpg"
        if len(files) == 1 and args.get("out"):
            out = _resolve(args["out"], cwd)
        elif out_dir is not None:
            out = out_dir / (f.stem + ext)
        else:
            out = f.with_suffix(ext) if ext.lower() != f.suffix.lower() else _default_out(f, cwd, "-small" if max_kb else "-converted", ext)
        before = f.stat().st_size
        if max_kb and FORMATS.get(out.suffix.lower()) in ("JPEG", "WEBP", "AVIF"):
            _fit_size(img, out, max_kb, strip, exif)
        elif max_kb:
            work = img
            _save(work, out, strip=strip, keep_exif=exif)
            while out.stat().st_size > max_kb * 1024 and min(work.size) > 32:
                work = work.resize((int(work.width * 0.85), int(work.height * 0.85)), _pil().LANCZOS)
                _save(work, out, strip=strip, keep_exif=exif)
        else:
            _save(img, out, args.get("quality"), strip, exif)
        done.append(out)
        lines.append(f"{f.name} ({_human(before)}) → {out.name} ({_human(out.stat().st_size)})")
    head = f"converted {len(done)} file(s)" + (" — metadata stripped" if strip else "")
    return head + "\n" + "\n".join(lines[:40]) + (f"\n… {len(lines) - 40} more" if len(lines) > 40 else ""), done


def _fit_size(img, out: Path, max_kb: float, strip: bool, exif) -> None:
    """The best quality (then the largest size) that fits max_kb."""
    work = img
    for _ in range(8):
        lo, hi, best = 30, 95, None
        while lo <= hi:
            q = (lo + hi) // 2
            _save(work, out, q, strip, exif)
            if out.stat().st_size <= max_kb * 1024:
                best, lo = q, q + 1
            else:
                hi = q - 1
        if best is not None:
            _save(work, out, best, strip, exif)
            return
        work = work.resize((int(work.width * 0.8), int(work.height * 0.8)), _pil().LANCZOS)
    _save(work, out, 30, strip, exif)


def gif(args: dict, cwd: str) -> tuple[str, list]:
    from PIL import Image
    files = _expand(args.get("paths"), cwd)
    if len(files) < 2:
        raise ImageError("gif needs at least two frames in paths (a list or a glob)")
    frames = [_rgba(_open(f)) for f in files[:600]]
    size = frames[0].size
    if args.get("width"):
        w = int(args["width"])
        size = (w, max(1, int(size[1] * w / size[0])))
    frames = [f.resize(size, Image.LANCZOS) if f.size != size else f for f in frames]
    out = _resolve(args["out"], cwd) if args.get("out") else Path(cwd) / "animation.gif"
    out.parent.mkdir(parents=True, exist_ok=True)
    pal = [f.convert("P", palette=Image.ADAPTIVE) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(args.get("duration", 120)),
                loop=int(args.get("loop", 0)), optimize=True)
    return f"GIF of {len(frames)} frames, {int(args.get('duration', 120))} ms each → {_summary(out)}", [out]


def qr(args: dict, cwd: str) -> tuple[str, list]:
    text = str(args.get("text") or "")
    if not text:
        raise ImageError("qr needs text (a URL, Wi-Fi string, contact, anything)")
    out = _resolve(args["out"], cwd) if args.get("out") else Path(cwd) / "qr.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    scale = int(args.get("size", 10))
    dark, light = args.get("color", "black"), args.get("background", "white")
    try:
        import segno
        segno.make(text, error="m").save(str(out), scale=scale, dark=dark, light=light, border=4)
    except ImportError:
        try:
            import qrcode
            img = qrcode.make(text, box_size=scale, border=4)
            img.save(out)
        except ImportError:
            raise ImageError("making QR codes needs segno — install it with: pip install segno "
                             "(or ask `have segno` for this machine's command)") from None
    img = _open(out)
    return f"QR code for {text[:60]!r} → {_summary(out, img)}", [out]


def icons(args: dict, cwd: str) -> tuple[str, list]:
    from PIL import Image
    path = _resolve(args.get("path"), cwd)
    src = _rgba(_open(path))
    side = max(src.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(src, ((side - src.width) // 2, (side - src.height) // 2))
    which = str(args.get("set", "web")).lower()
    sets = list(ICON_SETS) if which == "all" else [which]
    if any(s not in ICON_SETS for s in sets):
        raise ImageError(f"set must be one of {', '.join(ICON_SETS)} or all")
    out_dir = _resolve(args["out"], cwd) if args.get("out") else path.parent / f"{path.stem}-icons"
    made = []
    for s in sets:
        for n in ICON_SETS[s]:
            p = out_dir / s / f"icon-{n}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            square.resize((n, n), Image.LANCZOS).save(p, optimize=True)
            made.append(p)
    ico = out_dir / "favicon.ico"
    out_dir.mkdir(parents=True, exist_ok=True)
    square.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    made.append(ico)
    preview = [p for p in made if p.name in ("icon-512.png", "icon-1024.png", "icon-192.png")][:1]
    return (f"{len(made)} icons ({', '.join(sets)}) + favicon.ico → {out_dir}"
            + ("" if src.width == src.height else f" (the {src.width}×{src.height} source was centred on a square)"),
            preview + [ico])


OPS = {"info": info, "edit": edit, "create": create, "combine": combine, "diff": diff, "convert": convert,
       "gif": gif, "qr": qr, "icons": icons}


def run(args: dict, cwd: str) -> tuple[str, list, bool]:
    """(text, outputs, is_error) for the tool."""
    op = str(args.get("op") or ("edit" if args.get("steps") and args.get("path") else "info")).lower()
    fn = OPS.get(op)
    if fn is None:
        return f"error: op must be one of {', '.join(OPS)}", [], True
    try:
        text, outputs = fn(args, cwd)
    except ImageError as exc:
        return f"error: {exc}", [], True
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return f"error: {type(exc).__name__}: {exc}", [], True
    return text, outputs, False


DESCRIPTION = (
    "Look at, create and change images locally with Pillow — no vision model and no script needed; the result "
    "file is shown to the user. op=info (size, EXIF, colours, layout, OCR text) | edit (path + steps → out) | "
    "create (width, height, background + steps → out) | combine (paths/glob → grid|row|column sheet, labels) | "
    "diff (path vs other: changed regions boxed) | convert (path(s) → format, max_kb, strip metadata) | "
    "gif (frames → animation) | qr (text → QR code) | icons (path → favicon + web/ios/android sizes). "
    "steps: [{do: crop box=[x,y,w,h]|trim|aspect}, {do: resize width/height fit=contain|cover|stretch | scale}, "
    "{do: rotate degrees}, {do: flip direction}, {do: pad width height color}, "
    "{do: adjust brightness/contrast/saturation/sharpness/gamma (1=same)}, {do: filter name=blur|sharpen|smooth|"
    "detail|edges|emboss|contour radius}, grayscale, sepia, invert, {do: replace_color from to tolerance}, "
    "{do: transparent color}, {do: opacity value}, {do: round_corners radius}, {do: border width color}, "
    "{do: shadow}, {do: gradient from to direction}, {do: text text at='center'|'top'|'bottom-right'|'x,y' size "
    "color stroke background}, {do: shape kind=rect|ellipse|line|arrow box|points color fill width}, "
    "{do: watermark text|image at opacity}, {do: redact box mode=black|blur|pixelate}, {do: paste image at width}]. "
    "Colours: names, #hex or rgb(). Boxes may use percentages ('10%').")


def tool_def():
    from .tools import ToolDef, ToolResult
    step = {"type": "object", "additionalProperties": True,
            "properties": {"do": {"type": "string"}}, "required": ["do"]}
    params = {"type": "object", "properties": {
        "op": {"type": "string", "enum": list(OPS)},
        "path": {"type": "string", "description": "the input image (info, edit, diff, convert, icons)"},
        "paths": {"description": "several images: a list of paths or one glob (combine, gif, convert)",
                  "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "other": {"type": "string", "description": "diff: the second image"},
        "out": {"type": "string", "description": "output file (or folder for icons / batch convert); a sensible default otherwise"},
        "steps": {"type": "array", "items": step},
        "width": {"type": "integer"}, "height": {"type": "integer"},
        "background": {"type": "string"},
        "layout": {"type": "string", "enum": ["grid", "row", "column"]},
        "columns": {"type": "integer"}, "gap": {"type": "integer"}, "cell": {"type": "integer"},
        "labels": {"description": "combine: true for file names, or a list of captions",
                   "anyOf": [{"type": "boolean"}, {"type": "array", "items": {"type": "string"}}]},
        "format": {"type": "string", "description": "convert: png, jpg, webp, avif, gif, bmp, tiff, ico, pdf"},
        "quality": {"type": "integer"}, "max_kb": {"type": "number"},
        "strip": {"type": "boolean", "description": "drop EXIF/GPS metadata"},
        "threshold": {"type": "integer"}, "duration": {"type": "integer"}, "loop": {"type": "integer"},
        "text": {"type": "string"}, "size": {"type": "integer"}, "color": {"type": "string"},
        "set": {"type": "string", "enum": ["web", "ios", "android", "all"]},
    }, "required": ["op"]}

    def run_tool(args, ctx):
        text, outputs, is_error = run(args, ctx.cwd)
        return ToolResult(text=text, is_error=is_error, outputs=outputs)

    return ToolDef("image", DESCRIPTION, params, run=run_tool)
