"""Vision for text-only models: images in the history become a vision model's description before the request."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import digest, vision
from hexiel.config import ModelConfig
from tests.test_tool_models import session_with

PNG = base64.b64encode(b"\x89PNG fake").decode()
TEXT_ONLY = ModelConfig(name="big", provider="openai", model="qwen3-coder:30b")


def test_vision_guess_and_override():
    assert vision.sees_images(ModelConfig(name="a", provider="anthropic", model="claude-sonnet-5-5"))
    assert vision.sees_images(ModelConfig(name="b", provider="openai", model="qwen3-vl:8b"))
    assert not vision.sees_images(ModelConfig(name="c", provider="openai", model="qwen3-coder:30b"))
    assert not vision.sees_images(ModelConfig(name="c2", provider="openai", model="gpt-oss:120b"))
    assert vision.sees_images(ModelConfig(name="c3", provider="openai", model="my-custom-model"))  # unknown keeps images
    assert vision.sees_images(ModelConfig(name="d", provider="openai", model="qwen3-coder:30b", vision="yes"))
    assert not vision.sees_images(ModelConfig(name="e", provider="anthropic", model="claude-x", vision="no"))


def test_text_only_model_gets_the_image_as_text(tmp_path, monkeypatch):
    s, calls, events = session_with(tmp_path, monkeypatch, {"describe_image": {"models": ["small"]}},
                                   summary="a login form; red text 'password expired'")
    s.model_config = TEXT_ONLY
    s.messages = [{"role": "user", "content": "why can't I log in?",
                   "images": [{"media_type": "image/png", "data": PNG, "approx_tokens": 800}]}]
    assert vision.describe_history(s) == 1
    msg = s.messages[0]
    assert "images" not in msg
    assert "password expired" in msg["content"] and "text-only" in msg["content"]
    assert vision.describe_history(s) == 0 and len(calls) == 1          # described once, not on every request


def test_vision_model_keeps_the_image(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"describe_image": {"models": ["small"]}})
    s.model_config = ModelConfig(name="v", provider="openai", model="gemma4:12b")
    s.messages = [{"role": "user", "content": "look", "images": [{"media_type": "image/png", "data": PNG}]}]
    assert vision.describe_history(s) == 0 and s.messages[0]["images"] and not calls


def test_no_vision_model_falls_back_to_ocr_or_says_why(tmp_path, monkeypatch):
    s, calls, _ = session_with(tmp_path, monkeypatch, {"describe_image": {"models": ["missing"]}})
    s.model_config = TEXT_ONLY
    s.messages = [{"role": "user", "content": "look", "images": [{"media_type": "image/png", "data": PNG}]}]
    monkeypatch.setattr(vision, "ocr", lambda data, timeout=30.0: "ERROR 42: disk full")
    vision.describe_history(s)
    assert "disk full" in s.messages[0]["content"] and "tesseract" in s.messages[0]["content"]

    s.messages = [{"role": "user", "content": "look", "images": [{"media_type": "image/png", "data": PNG}]}]
    monkeypatch.setattr(vision, "ocr", lambda data, timeout=30.0: "")
    vision.describe_history(s)
    content = s.messages[0]["content"]
    assert "not visible to this model" in content and "no usable vision model" in content
    assert "images" not in s.messages[0]
