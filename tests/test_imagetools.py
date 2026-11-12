"""The `image` tool: every op on generated fixtures, no network, no vision model."""

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import imagetools as it


def make(tmp_path, name="a.png", size=(200, 120), color="white", box=None, box_color="red"):
    img = Image.new("RGB", size, color)
    if box:
        ImageDraw.Draw(img).rectangle(box, fill=box_color)
    p = tmp_path / name
    img.save(p)
    return p


def run(tmp_path, **args):
    text, outputs, err = it.run(args, str(tmp_path))
    assert not err, text
    return text, outputs


def test_info_reports_size_format_and_card(tmp_path):
    p = make(tmp_path, box=(10, 10, 60, 60))
    text, outputs = run(tmp_path, op="info", path=str(p))
    assert "200×120 PNG RGB" in text and "dominant colours" in text and outputs == []


def test_info_flags_gps_metadata(tmp_path):
    img = Image.new("RGB", (40, 40), "white")
    exif = img.getexif()
    exif[0x010F] = "PhoneCo"
    exif.get_ifd(0x8825)[1] = "N"
    p = tmp_path / "photo.jpg"
    img.save(p, exif=exif)
    text, _ = run(tmp_path, op="info", path=str(p))
    assert "PhoneCo" in text and "GPS location: present" in text


def test_edit_chain_writes_a_new_file_and_reports_it(tmp_path):
    p = make(tmp_path, box=(50, 30, 150, 90))
    text, outputs = run(tmp_path, op="edit", path=str(p), steps=[
        {"do": "crop", "box": [40, 20, 120, 80]},
        {"do": "resize", "width": 240},
        {"do": "text", "text": "Hello", "at": "top", "size": 20, "color": "black"},
        {"do": "border", "width": 4, "color": "#123456"},
        {"do": "shape", "kind": "arrow", "points": [[5, 5], [60, 60]], "color": "blue"},
    ])
    out = outputs[0]
    assert out.name == "a-edited.png" and out.exists()
    assert Image.open(out).size == (248, 168)
    assert "crop, resize, text, border, shape" in text and "248×168" in text
    assert Image.open(p).size == (200, 120)                          # the original is untouched


@pytest.mark.parametrize("step", [
    {"do": "rotate", "degrees": 90}, {"do": "flip", "direction": "vertical"}, {"do": "pad", "width": 300, "height": 300},
    {"do": "adjust", "brightness": 1.3, "contrast": 0.8, "saturation": 1.5, "gamma": 1.2},
    {"do": "filter", "name": "blur", "radius": 3}, {"do": "filter", "name": "edges"}, "grayscale", "sepia", "invert",
    {"do": "replace_color", "from": "red", "to": "green"}, {"do": "transparent", "color": "white"},
    {"do": "opacity", "value": 0.5}, {"do": "round_corners", "radius": 20}, {"do": "shadow"},
    {"do": "gradient", "from": "navy", "to": "orange"}, {"do": "watermark", "text": "© me"},
    {"do": "redact", "box": [0, 0, "50%", "50%"], "mode": "pixelate"}, {"do": "redact", "box": [0, 0, 20, 20], "mode": "blur"},
    {"do": "crop", "trim": True}, {"do": "crop", "aspect": "1:1"}, {"do": "resize", "scale": 0.5},
    {"do": "resize", "width": 100, "height": 100, "fit": "cover"},
    {"do": "shape", "kind": "ellipse", "box": [10, 10, 50, 50], "fill": "yellow"},
])
def test_every_step_runs(tmp_path, step):
    p = make(tmp_path, box=(50, 30, 150, 90))
    _, outputs = run(tmp_path, op="edit", path=str(p), steps=[step], out=str(tmp_path / "o.png"))
    assert Image.open(outputs[0]).size[0] > 0


def test_trim_and_aspect_do_what_they_say(tmp_path):
    p = make(tmp_path, box=(50, 30, 149, 89))
    _, o = run(tmp_path, op="edit", path=str(p), steps=[{"do": "crop", "trim": True}], out=str(tmp_path / "t.png"))
    assert Image.open(o[0]).size == (100, 60)
    _, o = run(tmp_path, op="edit", path=str(p), steps=[{"do": "crop", "aspect": "16:9"}], out=str(tmp_path / "w.png"))
    w, h = Image.open(o[0]).size
    assert abs(w / h - 16 / 9) < 0.02


def test_redact_really_removes_the_pixels(tmp_path):
    p = make(tmp_path, box=(0, 0, 50, 50), box_color="red")
    _, o = run(tmp_path, op="edit", path=str(p), steps=[{"do": "redact", "box": [0, 0, 60, 60]}], out=str(tmp_path / "r.png"))
    assert Image.open(o[0]).convert("RGB").getpixel((10, 10)) == (0, 0, 0)


def test_bad_steps_are_one_line_errors(tmp_path):
    p = make(tmp_path)
    text, _, err = it.run({"op": "edit", "path": str(p), "steps": [{"do": "explode"}]}, str(tmp_path))
    assert err and "unknown step 'explode'" in text
    text, _, err = it.run({"op": "edit", "path": str(p), "steps": [{"do": "crop", "box": [500, 500, 10, 10]}]}, str(tmp_path))
    assert err and "outside" in text
    text, _, err = it.run({"op": "info", "path": str(tmp_path / "nope.png")}, str(tmp_path))
    assert err and "no such image" in text
    text, _, err = it.run({"op": "fly"}, str(tmp_path))
    assert err and "op must be one of" in text


def test_create_a_banner(tmp_path):
    text, o = run(tmp_path, op="create", width=800, height=200, background="#0f172a", out="banner.png", steps=[
        {"do": "gradient", "from": "#1e3a8a", "to": "#9333ea", "direction": "horizontal"},
        {"do": "text", "text": "hexiel 0.9.7", "size": 64, "color": "white"},
    ])
    assert Image.open(o[0]).size == (800, 200) and o[0] == tmp_path / "banner.png"


def test_combine_grid_row_and_labels(tmp_path):
    files = [make(tmp_path, f"s{i}.png", box=(i * 10, 10, i * 10 + 30, 40)) for i in range(5)]
    text, o = run(tmp_path, op="combine", paths=str(tmp_path / "s*.png"), labels=True, out="sheet.png")
    assert "combined 5 images (grid, 3×2)" in text
    text, o = run(tmp_path, op="combine", paths=[str(files[0]), str(files[1])], layout="row", out="pair.png")
    assert Image.open(o[0]).size[0] > 2 * 200


def test_diff_finds_and_boxes_the_change(tmp_path):
    a = make(tmp_path, "before.png", size=(400, 300))
    b = make(tmp_path, "after.png", size=(400, 300), box=(300, 200, 340, 240))
    text, o = run(tmp_path, op="diff", path=str(a), other=str(b))
    assert "1 changed region" in text and "x=300 y=200" in text and o[0].name == "after-diff.png"
    text, o = run(tmp_path, op="diff", path=str(a), other=str(a))
    assert text.startswith("identical") and o == []


def test_convert_compress_and_strip(tmp_path):
    import random
    img = Image.new("RGB", (800, 600))
    img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(800 * 600)])
    exif = img.getexif()
    exif[0x010F] = "PhoneCo"
    p = tmp_path / "noisy.jpg"
    img.save(p, quality=95, exif=exif)
    text, o = run(tmp_path, op="convert", path=str(p), format="jpg", max_kb=120, strip=True)
    assert o[0].stat().st_size <= 120 * 1024 and "metadata stripped" in text
    assert "PhoneCo" not in str(dict(Image.open(o[0]).getexif()))
    text, o = run(tmp_path, op="convert", path=str(p), format="webp")
    assert o[0].suffix == ".webp"


def test_batch_convert_a_glob_into_a_folder(tmp_path):
    for i in range(3):
        make(tmp_path, f"b{i}.png")
    text, o = run(tmp_path, op="convert", paths="b*.png", format="jpg", out="jpgs")
    assert len(o) == 3 and all(p.parent.name == "jpgs" and p.suffix == ".jpg" for p in o)


def test_gif_from_frames(tmp_path):
    for i in range(4):
        make(tmp_path, f"f{i}.png", box=(i * 40, 20, i * 40 + 30, 50))
    text, o = run(tmp_path, op="gif", paths="f*.png", duration=80)
    assert Image.open(o[0]).n_frames == 4


def test_qr_needs_a_library_or_says_how_to_get_one(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_qr(name, *a, **k):
        if name in ("segno", "qrcode"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_qr)
    text, _, err = it.run({"op": "qr", "text": "https://hexiel.tech"}, str(tmp_path))
    assert err and "pip install segno" in text


def test_qr_when_segno_is_installed(tmp_path):
    pytest.importorskip("segno")
    text, o = run(tmp_path, op="qr", text="https://hexiel.tech")
    assert o[0].exists() and "QR code" in text


def test_icons_for_every_platform(tmp_path):
    p = make(tmp_path, "logo.png", size=(600, 400), box=(100, 100, 500, 300))
    text, o = run(tmp_path, op="icons", path=str(p), set="all")
    out = tmp_path / "logo-icons"
    assert (out / "favicon.ico").exists() and (out / "ios" / "icon-1024.png").exists()
    assert Image.open(out / "android" / "icon-192.png").size == (192, 192)
    assert "centred on a square" in text


def test_the_tool_reports_its_outputs_to_the_views(tmp_path):
    from hexiel.tools import ToolContext, Toolchain
    tool = it.tool_def()
    p = make(tmp_path)
    res = tool.run({"op": "edit", "path": str(p), "steps": [{"do": "grayscale"}]},
                   ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    assert not res.is_error and res.outputs and Path(res.outputs[0]).exists()


def test_session_announces_outputs_before_the_result(tmp_path):
    from tests.test_remote_control import make_session
    s = make_session(tmp_path)
    s.tools = [t for t in s.tools if t.name != "image"] + [it.tool_def()]
    s.settings.auto_approve = True
    s.auto_approve = True
    events = []
    s.on_event = lambda e, *p, **k: events.append((e, *p))
    p = make(tmp_path)
    call = {"id": "c1", "name": "image", "arguments": {"op": "edit", "path": str(p), "steps": ["sepia"]}}
    s.checkpoints.append([])
    s._execute_tool(call)
    names = [e[0] for e in events]
    assert "outputs" in names and names.index("outputs") < names.index("tool_result")
    assert s.messages[-1]["outputs"][0].endswith("a-edited.png")
