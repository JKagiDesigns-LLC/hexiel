"""The `video` tool: look at, cut, join, convert, shrink and make videos and audio locally with ffmpeg.

No model and no script is needed; the result file is shown in the chat. ffmpeg comes from PATH when there is one,
otherwise from the `imageio-ffmpeg` package that is a normal dependency of hexiel (so it works on every OS without
admin rights). ffprobe is never required: the facts are read from `ffmpeg -i`."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - fixed argv lists, never a shell
import tempfile
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
CODECS = {  # extension → ffmpeg codec options
    ".mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"],
    ".m4v": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    ".mov": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    ".mkv": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    ".webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-c:a", "libopus"],
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".m4a": ["-c:a", "aac"], ".aac": ["-c:a", "aac"],
    ".flac": ["-c:a", "flac"], ".wav": ["-c:a", "pcm_s16le"], ".ogg": ["-c:a", "libvorbis"], ".opus": ["-c:a", "libopus"],
}
TIMEOUT = 1800


class VideoError(ValueError):
    pass


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - missing package or no binary for this platform
        raise VideoError("ffmpeg is not available — `pip install imageio-ffmpeg` (bundled, no admin rights) "
                         "or install ffmpeg with your package manager") from None


def _resolve(path, cwd: str) -> Path:
    if not path:
        raise VideoError("a path is required")
    p = Path(str(path)).expanduser()
    p = p if p.is_absolute() else Path(cwd) / p
    if not p.exists():
        raise VideoError(f"no such file: {path}")
    return p


def _seconds(value, name="time") -> float | None:
    """'90', 90, '1:30', '00:01:30.5', '1m30s' → seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", text)
    if m and any(m.groups()) and not re.fullmatch(r"\d+(\.\d+)?", text):
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    try:
        parts = [float(x) for x in text.split(":")]
    except ValueError:
        raise VideoError(f"{name} '{value}' is not a time (use seconds, 1:30 or 00:01:30.5)") from None
    total = 0.0
    for x in parts:
        total = total * 60 + x
    return total


def _run(args: list[str], what: str) -> str:
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)  # nosec B603 - fixed argv
    except subprocess.TimeoutExpired:
        raise VideoError(f"{what} took longer than {TIMEOUT // 60} minutes and was stopped") from None
    if proc.returncode != 0:
        tail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()][-3:]
        raise VideoError(f"ffmpeg could not {what}: {' | '.join(tail) or 'unknown error'}")
    return proc.stderr or ""


def probe(path: Path) -> dict:
    """Facts from `ffmpeg -i` (no ffprobe needed)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,  # nosec B603
                          timeout=60)
    text = proc.stderr or ""
    info: dict = {"duration": None, "width": None, "height": None, "fps": None, "vcodec": None, "acodec": None,
                  "rate": None, "channels": None, "bitrate": None, "size": path.stat().st_size}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"bitrate:\s*(\d+) kb/s", text)
    if m:
        info["bitrate"] = int(m[1])
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line and not info["vcodec"]:
            info["vcodec"] = re.search(r"Video:\s*(\w+)", line)[1]
            dim = re.search(r"(\d{2,5})x(\d{2,5})", line.split("Video:")[1])
            if dim:
                info["width"], info["height"] = int(dim[1]), int(dim[2])
            fps = re.search(r"([\d.]+) fps", line)
            if fps:
                info["fps"] = float(fps[1])
        elif "Audio:" in line and not info["acodec"]:
            info["acodec"] = re.search(r"Audio:\s*(\w+)", line)[1]
            hz = re.search(r"(\d+) Hz", line)
            info["rate"] = int(hz[1]) if hz else None
            info["channels"] = ("mono" if "mono" in line else "stereo" if "stereo" in line else None)
    if not info["vcodec"] and not info["acodec"]:
        raise VideoError(f"{path.name} is not a media file ffmpeg can read")
    return info


def _clock(sec: float | None) -> str:
    if sec is None:
        return "?"
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def _describe(path: Path, info: dict | None = None) -> str:
    i = info or probe(path)
    bits = [_clock(i["duration"])]
    if i["vcodec"]:
        bits.append(f"{i['width']}×{i['height']}" + (f" {i['fps']:g} fps" if i["fps"] else "") + f" {i['vcodec']}")
    if i["acodec"]:
        bits.append(f"{i['acodec']}" + (f" {i['rate'] // 1000} kHz" if i["rate"] else "") + (f" {i['channels']}" if i["channels"] else ""))
    else:
        bits.append("no audio")
    bits.append(_human(i["size"]))
    return f"{path.name}: " + ", ".join(bits)


def _out(args: dict, cwd: str, src: Path | None, suffix: str, ext: str | None = None) -> Path:
    if args.get("out"):
        p = Path(str(args["out"])).expanduser()
        p = p if p.is_absolute() else Path(cwd) / p
    else:
        base = src or Path(cwd) / "video"
        p = base.with_name(f"{base.stem}-{suffix}{ext or base.suffix or '.mp4'}")
        if src and not args.get("out"):
            n = 2
            while p.exists() and p.resolve() == src.resolve():
                p = base.with_name(f"{base.stem}-{suffix}{n}{ext or base.suffix}")
                n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _codec(out: Path, quality: str | None = None) -> list[str]:
    opts = list(CODECS.get(out.suffix.lower(), []))
    if out.suffix.lower() in (".mp4", ".m4v", ".mov", ".mkv") and quality:
        crf = {"high": "18", "good": "23", "small": "28", "tiny": "32"}.get(str(quality), str(quality))
        opts += ["-crf", crf, "-preset", "medium"]
    return opts


def _finish(verb: str, out: Path) -> tuple[str, list]:
    return f"{verb} → {_describe(out)}", [out]


# ---------------------------------------------------------------- ops

def info(args: dict, cwd: str) -> tuple[str, list]:
    p = _resolve(args.get("path"), cwd)
    i = probe(p)
    lines = [_describe(p, i)]
    if i["bitrate"]:
        lines.append(f"overall bitrate {i['bitrate']} kb/s")
    return "\n".join(lines), []


def trim(args: dict, cwd: str) -> tuple[str, list]:
    p = _resolve(args.get("path"), cwd)
    start, end, dur = _seconds(args.get("start"), "start"), _seconds(args.get("end"), "end"), _seconds(args.get("duration"), "duration")
    if start is None and end is None and dur is None:
        raise VideoError("give start and end (or duration)")
    out = _out(args, cwd, p, "trimmed")
    cmd = []
    if start:
        cmd += ["-ss", f"{start}"]
    cmd += ["-i", str(p)]
    if dur is not None:
        cmd += ["-t", f"{dur}"]
    elif end is not None:
        cmd += ["-t", f"{max(0.1, end - (start or 0))}"]
    _run(cmd + _codec(out) + [str(out)], "trim the video")      # re-encoded, so the cut is frame-exact
    return _finish(f"trimmed {_clock(start or 0)}–{_clock(end) if end is not None else _clock((start or 0) + (dur or 0))}", out)


def join(args: dict, cwd: str) -> tuple[str, list]:
    raw = args.get("paths")
    items = [raw] if isinstance(raw, str) else list(raw or [])
    files: list[Path] = []
    for item in items:
        pat = Path(str(item)).expanduser()
        pat = pat if pat.is_absolute() else Path(cwd) / pat
        files += sorted(pat.parent.glob(pat.name)) if any(c in pat.name for c in "*?[") else [_resolve(item, cwd)]
    if len(files) < 2:
        raise VideoError("paths must name at least two files (or a glob that matches them)")
    out = _out(args, cwd, None, "joined", files[0].suffix) if not args.get("out") else _out(args, cwd, None, "")
    if not args.get("out"):
        out = files[0].with_name(f"joined{files[0].suffix}")
    infos = [probe(f) for f in files]
    has_video = all(i["vcodec"] for i in infos)
    has_audio = all(i["acodec"] for i in infos)
    w = int(args.get("width") or infos[0]["width"] or 0)
    h = int(args.get("height") or infos[0]["height"] or 0)
    cmd: list[str] = []
    for f in files:
        cmd += ["-i", str(f)]
    parts = []
    for n in range(len(files)):
        if has_video:
            parts.append(f"[{n}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                         f"setsar=1,fps=30,format=yuv420p[v{n}]")
        if has_audio:
            parts.append(f"[{n}:a]aresample=44100,aformat=channel_layouts=stereo[a{n}]")
    chain = "".join((f"[v{n}]" if has_video else "") + (f"[a{n}]" if has_audio else "") for n in range(len(files)))
    parts.append(f"{chain}concat=n={len(files)}:v={1 if has_video else 0}:a={1 if has_audio else 0}"
                 f"{'[v]' if has_video else ''}{'[a]' if has_audio else ''}")
    maps = (["-map", "[v]"] if has_video else []) + (["-map", "[a]"] if has_audio else [])
    _run(cmd + ["-filter_complex", ";".join(parts), *maps, *_codec(out), str(out)], "join the clips")
    return _finish(f"joined {len(files)} clips", out)


def convert(args: dict, cwd: str) -> tuple[str, list]:
    p = _resolve(args.get("path"), cwd)
    fmt = str(args.get("format") or "").lower().lstrip(".")
    if not fmt and not args.get("out"):
        raise VideoError("give a format (mp4, webm, mkv, mov, gif, mp3, wav, m4a, flac, ogg, opus) or an out path")
    out = _out(args, cwd, p, "converted", f".{fmt}" if fmt else None)
    if out.suffix.lower() == ".gif":
        return gif({**args, "path": str(p), "out": str(out)}, cwd)
    _run(["-i", str(p), *_codec(out, args.get("quality")), str(out)], f"convert to {out.suffix[1:]}")
    return _finish(f"converted to {out.suffix[1:]}", out)


def compress(args: dict, cwd: str) -> tuple[str, list]:
    p = _resolve(args.get("path"), cwd)
    i = probe(p)
    out = _out(args, cwd, p, "small", ".mp4" if p.suffix.lower() not in (".mp4", ".webm", ".mkv", ".mov") else None)
    max_mb = args.get("max_mb")
    if max_mb:
        if not i["duration"]:
            raise VideoError("cannot aim for a size: the duration is unknown")
        total_kbps = float(max_mb) * 8192 / i["duration"] * 0.95
        abr = 96 if i["acodec"] else 0
        vbr = max(50, int(total_kbps - abr))
        passlog = str(Path(tempfile.mkdtemp(prefix="hexiel-pass-")) / "pass")
        base = ["-i", str(p), "-c:v", "libx264", "-b:v", f"{vbr}k", "-pix_fmt", "yuv420p"]
        if args.get("width"):
            base += ["-vf", f"scale={int(args['width'])}:-2"]
        _run(base + ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", "-"], "analyse the video")
        audio = ["-c:a", "aac", "-b:a", f"{abr}k"] if abr else ["-an"]
        _run(base + ["-pass", "2", "-passlogfile", passlog, *audio, "-movflags", "+faststart", str(out)], "compress the video")
        shutil.rmtree(Path(passlog).parent, ignore_errors=True)
    else:
        vf = ["-vf", f"scale='min({int(args['width'])},iw)':-2"] if args.get("width") else []
        _run(["-i", str(p), *vf, *_codec(out, args.get("quality") or "small"), str(out)], "compress the video")
    before, after = p.stat().st_size, out.stat().st_size
    text, outs = _finish(f"compressed {_human(before)} → {_human(after)}", out)
    return text, outs


def edit(args: dict, cwd: str) -> tuple[str, list]:
    """resize/crop/rotate/speed/mute/text/subtitles/fade in one pass: a list of steps."""
    p = _resolve(args.get("path"), cwd)
    steps = args.get("steps") or []
    if not steps:
        raise VideoError("steps is empty — e.g. [{do:'resize', width:1280}, {do:'speed', factor:2}]")
    i = probe(p)
    vf: list[str] = []
    af: list[str] = []
    mute = False
    used = []
    for st in steps:
        st = {"do": st} if isinstance(st, str) else dict(st)
        do = str(st.get("do", "")).lower()
        used.append(do)
        if do == "resize":
            w, h = st.get("width"), st.get("height")
            if not (w or h):
                raise VideoError("resize needs width and/or height")
            vf.append(f"scale={int(w) if w else -2}:{int(h) if h else -2}")
        elif do == "crop":
            if st.get("aspect"):
                a, b = (float(x) for x in str(st["aspect"]).split(":"))
                vf.append(f"crop='min(iw,ih*{a}/{b})':'min(ih,iw*{b}/{a})'")
            else:
                vf.append(f"crop={int(st['width'])}:{int(st['height'])}:{int(st.get('x', 0))}:{int(st.get('y', 0))}")
        elif do == "rotate":
            deg = int(st.get("degrees", 90)) % 360
            vf.append({90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}.get(deg, "null"))
        elif do == "flip":
            vf.append("vflip" if str(st.get("direction", "horizontal")).startswith("v") else "hflip")
        elif do == "speed":
            f = float(st.get("factor", 1))
            if not 0.1 <= f <= 16:
                raise VideoError("speed factor must be between 0.1 and 16")
            vf.append(f"setpts=PTS/{f}")
            rest = f
            while rest > 2.0:
                af.append("atempo=2.0")
                rest /= 2.0
            while rest < 0.5:
                af.append("atempo=0.5")
                rest /= 0.5
            af.append(f"atempo={rest}")
        elif do == "fade":
            d = float(st.get("duration", 1))
            total = i["duration"] or 0
            vf += [f"fade=t=in:st=0:d={d}", f"fade=t=out:st={max(0, total - d)}:d={d}"]
            af += [f"afade=t=in:st=0:d={d}", f"afade=t=out:st={max(0, total - d)}:d={d}"]
        elif do == "mute":
            mute = True
        elif do == "volume":
            af.append(f"volume={float(st.get('factor', 1))}")
        elif do == "grayscale":
            vf.append("hue=s=0")
        elif do == "text":
            txt = str(st.get("text", "")).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            x = {"center": "(w-text_w)/2", "left": "20", "right": "w-text_w-20"}.get(str(st.get("x", "center")), str(st.get("x", "(w-text_w)/2")))
            y = {"top": "30", "center": "(h-text_h)/2", "bottom": "h-text_h-40"}.get(str(st.get("at", "bottom")), "h-text_h-40")
            size = int(st.get("size", 48))
            color = str(st.get("color", "white"))
            box = ":box=1:boxcolor=black@0.5:boxborderw=12" if st.get("background", True) else ""
            when = ""
            if st.get("start") is not None or st.get("end") is not None:
                when = f":enable='between(t,{_seconds(st.get('start')) or 0},{_seconds(st.get('end')) or 99999})'"
            font = _font_option()
            vf.append(f"drawtext={font}expansion=none:text='{txt}':fontsize={size}:fontcolor={color}:x={x}:y={y}{box}{when}")
        elif do == "subtitles":
            srt = _resolve(st.get("file"), cwd)
            esc = str(srt).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            vf.append(f"subtitles='{esc}'")
        else:
            raise VideoError(f"unknown step '{do}' — resize, crop, rotate, flip, speed, fade, mute, volume, grayscale, text, subtitles")
    out = _out(args, cwd, p, "edited")
    cmd = ["-i", str(p)]
    if vf and i["vcodec"]:
        cmd += ["-vf", ",".join(vf)]
    if mute:
        cmd += ["-an"]
    elif af and i["acodec"]:
        cmd += ["-af", ",".join(af)]
    _run(cmd + _codec(out, args.get("quality")) + [str(out)], "apply the edits")
    return _finish(f"edited ({', '.join(used)})", out)


def _font_option() -> str:
    for cand in ("/usr/share/fonts/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(cand).exists():
            return "fontfile='" + cand.replace(":", "\\:") + "':"
    try:
        from PIL import ImageFont
        path = getattr(ImageFont.load_default(size=20), "path", None)
        if path:
            return "fontfile='" + str(path).replace(":", "\\:") + "':"
    except Exception:  # noqa: BLE001
        pass
    return ""


def audio(args: dict, cwd: str) -> tuple[str, list]:
    """extract the sound track, replace it, or normalise loudness."""
    p = _resolve(args.get("path"), cwd)
    mode = str(args.get("mode") or "extract").lower()
    if mode == "extract":
        fmt = str(args.get("format") or "mp3").lstrip(".")
        out = _out(args, cwd, p, "audio", f".{fmt}")
        _run(["-i", str(p), "-vn", *_codec(out), str(out)], "extract the audio")
        return _finish("extracted the audio", out)
    if mode == "normalize":
        out = _out(args, cwd, p, "normalized")
        _run(["-i", str(p), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", *(["-c:v", "copy"] if probe(p)["vcodec"] else ["-vn"]),
              *[o for o in _codec(out) if o not in ("-c:v", "libx264", "-pix_fmt", "yuv420p")][:0], str(out)], "normalise the loudness")
        return _finish("normalised loudness (−16 LUFS)", out)
    if mode == "replace":
        track = _resolve(args.get("track"), cwd)
        out = _out(args, cwd, p, "dubbed")
        _run(["-i", str(p), "-i", str(track), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)],
             "replace the audio")
        return _finish("replaced the audio track", out)
    raise VideoError("audio mode must be extract, normalize or replace")


def gif(args: dict, cwd: str) -> tuple[str, list]:
    p = _resolve(args.get("path"), cwd)
    out = _out(args, cwd, p, "clip", ".gif")
    width = int(args.get("width") or 480)
    fps = int(args.get("fps") or 12)
    start, end = _seconds(args.get("start")), _seconds(args.get("end"))
    pre = (["-ss", f"{start}"] if start else [])
    dur = (["-t", f"{end - (start or 0)}"] if end else (["-t", "10"] if probe(p)["duration"] and probe(p)["duration"] > 12 else []))
    chain = f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"
    _run([*pre, "-i", str(p), *dur, "-vf", chain, "-loop", "0", str(out)], "make the GIF")
    return _finish("made a GIF", out)


def slideshow(args: dict, cwd: str) -> tuple[str, list]:
    raw = args.get("paths")
    items = [raw] if isinstance(raw, str) else list(raw or [])
    images: list[Path] = []
    for item in items:
        pat = Path(str(item)).expanduser()
        pat = pat if pat.is_absolute() else Path(cwd) / pat
        images += sorted(pat.parent.glob(pat.name)) if any(c in pat.name for c in "*?[") else [_resolve(item, cwd)]
    if not images:
        raise VideoError("paths must name the images (or a glob)")
    each = float(args.get("seconds") or 3)
    w, h = int(args.get("width") or 1280), int(args.get("height") or 720)
    out = _out(args, cwd, None, "slideshow", ".mp4") if args.get("out") else Path(cwd) / "slideshow.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = []
    for img in images:
        cmd += ["-loop", "1", "-t", f"{each}", "-i", str(img)]
    track = _resolve(args["track"], cwd) if args.get("track") else None
    if track:
        cmd += ["-i", str(track)]
    n = len(images)
    parts = [f"[{k}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{k}]"
             for k in range(n)]
    parts.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[v]")
    maps = ["-map", "[v]"] + (["-map", f"{n}:a", "-c:a", "aac", "-shortest"] if track else [])
    _run(cmd + ["-filter_complex", ";".join(parts), *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
         "make the slideshow")
    return _finish(f"slideshow of {n} images", out)


def frames(args: dict, cwd: str) -> tuple[str, list]:
    """Still images from a video: one poster frame, every N seconds, or a contact sheet."""
    p = _resolve(args.get("path"), cwd)
    i = probe(p)
    if not i["vcodec"]:
        raise VideoError(f"{p.name} has no video to take frames from")
    folder = Path(str(args["out"])).expanduser() if args.get("out") else p.with_name(f"{p.stem}-frames")
    folder = folder if folder.is_absolute() else Path(cwd) / folder
    mode = str(args.get("mode") or "poster")
    if mode == "poster":
        t = _seconds(args.get("at")) if args.get("at") is not None else min((i["duration"] or 2) / 3, 5.0)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{p.stem}-poster.jpg"
        _run(["-ss", f"{t}", "-i", str(p), "-frames:v", "1", "-q:v", "2", str(out)], "take a frame")
        return f"poster frame at {_clock(t)} → {out.name}", [out]
    every = float(args.get("every") or 10)
    folder.mkdir(parents=True, exist_ok=True)
    pattern = folder / f"{p.stem}-%03d.jpg"
    _run(["-i", str(p), "-vf", f"fps=1/{every}", "-q:v", "3", str(pattern)], "take the frames")
    made = sorted(folder.glob(f"{p.stem}-*.jpg"))
    if mode == "sheet":
        try:
            from . import imagetools
            text, outs, err = imagetools.run({"op": "combine", "paths": [str(m) for m in made], "labels": False,
                                              "cell": 320, "out": str(folder / f"{p.stem}-sheet.png")}, cwd)
            if not err:
                return f"contact sheet of {len(made)} frames (one every {every:g} s)", outs
        except Exception:  # noqa: BLE001
            pass
    return f"{len(made)} frames (one every {every:g} s) in {folder.name}/", made[:12]


OPS = {"info": info, "trim": trim, "join": join, "convert": convert, "compress": compress, "edit": edit,
       "audio": audio, "gif": gif, "slideshow": slideshow, "frames": frames}


def run(args: dict, cwd: str) -> tuple[str, list, bool]:
    """(text, outputs, is_error) for the tool."""
    op = str(args.get("op") or "info").lower()
    fn = OPS.get(op)
    if fn is None:
        return f"error: op must be one of {', '.join(OPS)}", [], True
    try:
        text, outputs = fn(args, cwd)
    except VideoError as exc:
        return f"error: {exc}", [], True
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return f"error: {type(exc).__name__}: {exc}", [], True
    return text, outputs, False


DESCRIPTION = (
    "Look at, cut, join, convert, shrink and make videos and audio locally with ffmpeg — no script needed; the result "
    "file plays in the chat. op=info (length, size, codecs) | trim (start, end|duration) | join (paths/glob → one "
    "video) | convert (format mp4|webm|mkv|mov|gif|mp3|wav|m4a|flac|ogg|opus, quality=high|good|small|tiny) | "
    "compress (max_mb aims for a file size, or width) | edit (steps: resize, crop, rotate, flip, speed factor, fade, "
    "mute, volume, grayscale, text, subtitles file) | audio (mode=extract|normalize|replace track) | "
    "gif (path, start, end, width, fps) | slideshow (images → video, seconds each, optional track) | "
    "frames (mode=poster|every|sheet, at, every). Times are seconds, 1:30 or 00:01:30.5.")


def tool_def():
    from .tools import ToolDef, ToolResult
    step = {"type": "object", "additionalProperties": True, "properties": {"do": {"type": "string"}}, "required": ["do"]}
    params = {"type": "object", "properties": {
        "op": {"type": "string", "enum": list(OPS)},
        "path": {"type": "string", "description": "the input video or audio file"},
        "paths": {"description": "several files: a list or one glob (join, slideshow)",
                  "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "out": {"type": "string", "description": "output file (or folder for frames); a sensible default otherwise"},
        "start": {"type": ["string", "number"]}, "end": {"type": ["string", "number"]}, "duration": {"type": ["string", "number"]},
        "format": {"type": "string"}, "quality": {"type": "string"}, "max_mb": {"type": "number"},
        "width": {"type": "integer"}, "height": {"type": "integer"}, "fps": {"type": "integer"},
        "steps": {"type": "array", "items": step},
        "mode": {"type": "string"}, "track": {"type": "string"}, "seconds": {"type": "number"},
        "at": {"type": ["string", "number"]}, "every": {"type": "number"},
    }, "required": ["op"]}

    def run_tool(args, ctx):
        text, outputs, is_error = run(args, ctx.cwd)
        return ToolResult(text=text, is_error=is_error, outputs=outputs)

    return ToolDef("video", DESCRIPTION, params, run=run_tool)
