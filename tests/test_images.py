"""Images typed in a user message attach directly (no view_image round-trip),
reach both provider formats, and are elided from stale history."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.llm import AnthropicProvider, OpenAICompatProvider
from hexiel.tools import attach_images

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def test_attach_finds_relative_at_and_quoted_paths(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "my pics").mkdir()
    (tmp_path / "my pics" / "b.png").write_bytes(PNG)
    text = 'what is in @shot.png, and "my pics/b.png"? also shot.png again, ignore missing.jpg'
    images, notes = attach_images(text, str(tmp_path))
    assert len(images) == 2                        # deduped; missing file skipped
    assert images[0]["media_type"] == "image/png"
    assert "1x1 png" in notes[0]


def test_attach_ignores_non_images_and_plain_text(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "fake.png").write_text("not really a png")
    images, notes = attach_images("read notes.txt and fake.png", str(tmp_path))
    assert images == [] and notes == []


def test_user_images_convert_for_both_providers():
    img = {"media_type": "image/png", "data": "AAAA", "approx_tokens": 100}
    msgs = [{"role": "user", "content": "describe", "images": [img]}]
    oa = OpenAICompatProvider._convert_messages(msgs)[0]["content"]
    assert oa[0] == {"type": "text", "text": "describe"}
    assert oa[1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    an = AnthropicProvider._convert_messages(msgs)[0]["content"]
    assert an[0]["type"] == "image" and an[0]["source"]["data"] == "AAAA"
    assert an[-1] == {"type": "text", "text": "describe"}


def test_stale_images_are_elided(tmp_path):
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://x"),
                Settings(), str(tmp_path), [], "sys")
    img = {"media_type": "image/png", "data": "AAAA", "approx_tokens": 100}
    s.messages = [
        {"role": "user", "content": "old", "images": [img]},
        {"role": "assistant", "content": "seen it"},
        {"role": "user", "content": "mid"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "new", "images": [img]},
        {"role": "assistant", "content": "ok"},
    ]
    assert s._elide_stale_tools() >= 1
    assert "images" not in s.messages[0] and "image elided" in s.messages[0]["content"]
    assert s.messages[4].get("images")             # current turn keeps its image
