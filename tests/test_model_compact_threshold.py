"""Per-model compaction point: a profile's own compact_threshold beats the global one."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel.config import ModelConfig
from hexiel import settings_edit as SE


def test_share_falls_back_to_global():
    m = ModelConfig(name="a", provider="openai", model="x")
    assert m.compact_share(0.6) == 0.6


def test_share_uses_profile_value():
    m = ModelConfig(name="a", provider="openai", model="x", compact_threshold=0.8)
    assert m.compact_share(0.6) == 0.8


def test_out_of_range_ignored():
    m = ModelConfig(name="a", provider="openai", model="x", compact_threshold=1.5)
    assert m.compact_share(0.6) == 0.6


def test_coerce_keeps_fraction():
    assert SE.coerce("model.compact_threshold", "0.7") == 0.7
