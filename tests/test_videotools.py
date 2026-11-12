"""The `video` tool on tiny ffmpeg-generated clips: no network, no model."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import videotools as vt

try:
    FFMPEG = vt.ffmpeg_exe()
except vt.VideoError:
    FFMPEG = None
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg")


def clip(tmp_path, name="a.mp4", seconds=3, size="320x180", audio=True, pattern="testsrc"):
    p = tmp_path / name
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=15:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def run(tmp_path, **args):
    text, outputs, err = vt.run(args, str(tmp_path))
    assert not err, text
    return text, outputs


def test_info(tmp_path):
    p = clip(tmp_path)
    text, outs = run(tmp_path, op="info", path=str(p))
    assert "320×180" in text and "h264" in text and "aac" in text and "0:03" in text and outs == []


def test_trim_is_the_right_length(tmp_path):
    p = clip(tmp_path, seconds=4)
    _, outs = run(tmp_path, op="trim", path=str(p), start="0:01", end="0:03")
    assert abs(vt.probe(outs[0])["duration"] - 2) < 0.4 and outs[0].name == "a-trimmed.mp4"
    assert abs(vt.probe(p)["duration"] - 4) < 0.4                      # the original is untouched


def test_join_scales_clips_to_the_first(tmp_path):
    a, b = clip(tmp_path, "a.mp4", 2), clip(tmp_path, "b.mp4", 2, size="160x90")
    _, outs = run(tmp_path, op="join", paths=[str(a), str(b)], out="both.mp4")
    i = vt.probe(outs[0])
    assert (i["width"], i["height"]) == (320, 180) and abs(i["duration"] - 4) < 0.6


def test_convert_and_audio_extract(tmp_path):
    p = clip(tmp_path)
    _, outs = run(tmp_path, op="convert", path=str(p), format="webm")
    assert vt.probe(outs[0])["vcodec"] == "vp9"
    _, outs = run(tmp_path, op="audio", path=str(p), mode="extract", format="mp3")
    i = vt.probe(outs[0])
    assert i["acodec"] == "mp3" and not i["vcodec"]


def test_edit_steps(tmp_path):
    p = clip(tmp_path)
    text, outs = run(tmp_path, op="edit", path=str(p), steps=[
        {"do": "resize", "width": 160}, {"do": "speed", "factor": 2}, {"do": "text", "text": "Hello: 50%"}, {"do": "mute"}])
    i = vt.probe(outs[0])
    assert i["width"] == 160 and not i["acodec"] and abs(i["duration"] - 1.5) < 0.4
    assert "resize, speed, text, mute" in text


def test_compress_to_a_size(tmp_path):
    p = clip(tmp_path, seconds=6, size="640x360", pattern="testsrc2")
    _, outs = run(tmp_path, op="compress", path=str(p), max_mb=0.15, out="small.mp4")
    assert outs[0].stat().st_size < 0.2 * 1024 * 1024


def test_gif_slideshow_and_frames(tmp_path):
    p = clip(tmp_path)
    _, g = run(tmp_path, op="gif", path=str(p), width=120)
    assert g[0].suffix == ".gif"
    from PIL import Image
    for i in range(3):
        Image.new("RGB", (200, 100), (i * 80, 40, 90)).save(tmp_path / f"s{i}.png")
    _, s = run(tmp_path, op="slideshow", paths="s*.png", seconds=1, width=320, height=180)
    assert abs(vt.probe(s[0])["duration"] - 3) < 0.6
    _, f = run(tmp_path, op="frames", path=str(p), mode="poster")
    assert f[0].suffix == ".jpg" and f[0].exists()
    text, f = run(tmp_path, op="frames", path=str(p), mode="sheet", every=1)
    assert f and f[0].suffix == ".png"


def test_errors_are_one_line(tmp_path):
    text, _, err = vt.run({"op": "info", "path": "nope.mp4"}, str(tmp_path))
    assert err and "no such file" in text
    (tmp_path / "x.txt").write_text("hi")
    text, _, err = vt.run({"op": "info", "path": "x.txt"}, str(tmp_path))
    assert err and "not a media file" in text
    p = clip(tmp_path)
    text, _, err = vt.run({"op": "edit", "path": str(p), "steps": [{"do": "explode"}]}, str(tmp_path))
    assert err and "unknown step" in text
    text, _, err = vt.run({"op": "trim", "path": str(p)}, str(tmp_path))
    assert err and "start" in text


def test_time_formats():
    assert vt._seconds("1:30") == 90 and vt._seconds("00:01:30.5") == 90.5 and vt._seconds("1m30s") == 90 and vt._seconds(7) == 7


def test_the_tool_reports_its_outputs(tmp_path):
    from hexiel.tools import ToolContext, Toolchain
    p = clip(tmp_path)
    res = vt.tool_def().run({"op": "gif", "path": str(p)}, ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert not res.is_error and res.outputs


def test_videos_play_in_the_views(tmp_path):
    from hexiel import media
    p = clip(tmp_path)
    info = media.describe(p)
    assert info["kind"] == "video" and info["duration"] > 2 and info["poster"] and (info["w"], info["h"]) == (320, 180)
    assert "🎬" in media.label(info) and "0:03" in media.label(info)
    assert media.resolve(info["poster"]).suffix == ".jpg"
    wav = tmp_path / "a.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(wav)], check=True)
    assert media.describe(wav)["kind"] == "audio"
    from hexiel.webui import media_items
    assert media_items([p])[0]["poster"] == info["poster"]
