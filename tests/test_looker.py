"""The local eyes: the pure parts always, the model itself only when its weights are on this machine."""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import looker, vision

REAL = looker.model_dir()                 # resolved at import: the conftest later points HEXIEL_DATA at a temp dir
HAVE_REAL = not looker.runtime_missing() and looker.installed()


def test_no_three_gram_repeats():
    assert looker._repeats([5, 6, 7, 5, 6]) == {7}
    assert looker._repeats([1, 2]) == set()


def test_object_tokens_become_pixel_boxes():
    from PIL import Image

    class Fake(looker.Looker):
        def __init__(self):
            pass

        def generate(self, img, task, max_tokens=120):
            return [2]

        class tok:
            @staticmethod
            def decode(ids, skip_special_tokens=False):
                return "cat<loc_100><loc_200><loc_500><loc_600>dog<loc_0><loc_0><loc_1000><loc_1000>"
    found = Fake().objects(Image.new("RGB", (1000, 500)))
    assert found[0] == ("cat", (100, 100, 500, 300)) and found[1][0] == "dog"


def test_the_card_uses_the_local_description_and_skips_whole_picture_objects(monkeypatch, tmp_path):
    from PIL import Image

    class Eyes:
        def see(self, img, task):
            return "a red square on a white background"

        def objects(self, img):
            return [("poster", (0, 0, 100, 100)), ("square", (10, 10, 40, 40))]
    monkeypatch.setattr(looker, "runtime_missing", lambda: "")
    monkeypatch.setattr(looker, "installed", lambda: True)
    monkeypatch.setattr(looker, "get", lambda **k: Eyes())
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buf, "PNG")
    card = vision.image_card(buf.getvalue())
    assert "it shows (local vision model): a red square on a white background" in card
    assert "square (top-left, 30% wide)" in card and "poster" not in card


def test_without_the_model_the_card_says_how_to_get_it(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(looker, "installed", lambda: False)
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(buf, "PNG")
    assert "hexiel --get-vision" in vision.image_card(buf.getvalue())


def test_the_model_is_never_downloaded_by_a_card(monkeypatch):
    called = []
    monkeypatch.setattr(looker, "download", lambda *a, **k: called.append(1))
    monkeypatch.setattr(looker, "installed", lambda: False)
    assert vision.local_description(b"not even an image") == [] and not called


@pytest.mark.skipif(not HAVE_REAL, reason="local vision model not on this machine")
def test_the_real_model_describes_a_picture():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 480), (135, 190, 235))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 330, 640, 480), fill=(60, 150, 60))
    d.ellipse((470, 40, 560, 130), fill=(255, 220, 60))
    text = looker.Looker(REAL).see(img, "caption").lower()
    assert len(text) > 10
