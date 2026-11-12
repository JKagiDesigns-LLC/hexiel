"""A local pair of eyes for models that cannot see: Florence-2 (230M parameters) run with onnxruntime on the CPU.

It turns a picture into words — a caption, a detailed description, the objects with where they are, the text — so a
text-only model can be handed the description instead of the image. Nothing leaves the machine. The weights (about
265 MB, int8) are downloaded once to the hexiel data directory the first time they are needed; install the runtime
with `pip install 'hexiel[see]'` (onnxruntime, tokenizers, numpy)."""

from __future__ import annotations

import re
import threading
from pathlib import Path

REPO = "onnx-community/Florence-2-base"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/main/"
FILES = ["onnx/vision_encoder_int8.onnx", "onnx/embed_tokens_int8.onnx", "onnx/encoder_model_int8.onnx",
         "onnx/decoder_model_merged_int8.onnx", "tokenizer.json"]
PROMPTS = {
    "caption": "What does the image describe?",
    "detailed": "Describe in detail what is shown in the image.",
    "paragraph": "Describe with a paragraph what is shown in the image.",
    "objects": "Locate the objects with category name in the image.",
    "text": "What is the text in the image?",
}
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
LAYERS, HEADS, HEAD_DIM = 6, 12, 64
_lock = threading.Lock()
_model: "Looker | None" = None


class LookerUnavailable(RuntimeError):
    pass


def model_dir() -> Path:
    from .config import home_data_dir
    return Path(home_data_dir()) / "models" / "florence-2-base"


def installed() -> bool:
    d = model_dir()
    return all((d / f).is_file() for f in FILES)


def runtime_missing() -> str:
    """'' when onnxruntime/tokenizers/numpy import, else what to install."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError as exc:
        return f"{exc.name} is not installed — pip install 'hexiel[see]'"
    return ""


def download(progress=None) -> Path:
    """Fetch the weights once (resumable by file). `progress(name, done, total)` is called as bytes arrive."""
    import httpx
    d = model_dir()
    for name in FILES:
        target = d / name
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        with httpx.stream("GET", BASE_URL + name, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            total, done = int(r.headers.get("content-length", 0)), 0
            with open(part, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(name, done, total)
        part.replace(target)
    return d


class Looker:
    def __init__(self, directory: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        mk = lambda n: ort.InferenceSession(str(directory / "onnx" / f"{n}_int8.onnx"), opts,  # noqa: E731
                                            providers=["CPUExecutionProvider"])
        self.vision, self.embed = mk("vision_encoder"), mk("embed_tokens")
        self.encoder, self.decoder = mk("encoder_model"), mk("decoder_model_merged")
        self.tok = Tokenizer.from_file(str(directory / "tokenizer.json"))

    # -- pieces
    def _pixels(self, img):
        import numpy as np
        from PIL import Image
        img = img.convert("RGB").resize((768, 768), Image.BICUBIC)
        a = np.asarray(img, dtype=np.float32) / 255.0
        a = (a - np.array(MEAN, dtype=np.float32)) / np.array(STD, dtype=np.float32)
        return a.transpose(2, 0, 1)[None]

    def _ids(self, text: str):
        import numpy as np
        return np.array([self.tok.encode(text).ids], dtype=np.int64)

    def generate(self, img, task: str, max_tokens: int = 120):
        """Greedy decoding (no 3-gram repeats). Returns the raw token ids, special tokens included."""
        import numpy as np
        feats = self.vision.run(None, {"pixel_values": self._pixels(img)})[0]
        prompt = self.embed.run(None, {"input_ids": self._ids(PROMPTS[task])})[0]
        embeds = np.concatenate([feats, prompt], axis=1)
        mask = np.ones(embeds.shape[:2], dtype=np.int64)
        enc = self.encoder.run(None, {"attention_mask": mask, "inputs_embeds": embeds})[0]
        names = [o.name for o in self.decoder.get_outputs()]
        past = {f"past_key_values.{i}.{k}.{kv}": np.zeros((1, HEADS, 0, HEAD_DIM), dtype=np.float32)
                for i in range(LAYERS) for k in ("decoder", "encoder") for kv in ("key", "value")}
        out_ids: list[int] = [2]
        step_ids = [2]
        for step in range(max_tokens):
            emb = self.embed.run(None, {"input_ids": np.array([[step_ids[-1]]], dtype=np.int64)})[0]
            feed = {"encoder_attention_mask": mask, "encoder_hidden_states": enc, "inputs_embeds": emb,
                    "use_cache_branch": np.array([step > 0]), **past}
            res = dict(zip(names, self.decoder.run(None, feed)))
            logits = res["logits"][0, -1]
            for i in range(LAYERS):
                past[f"past_key_values.{i}.decoder.key"] = res[f"present.{i}.decoder.key"]
                past[f"past_key_values.{i}.decoder.value"] = res[f"present.{i}.decoder.value"]
                if step == 0:
                    past[f"past_key_values.{i}.encoder.key"] = res[f"present.{i}.encoder.key"]
                    past[f"past_key_values.{i}.encoder.value"] = res[f"present.{i}.encoder.value"]
            for banned in _repeats(out_ids):
                logits[banned] = -1e9
            nxt = int(logits.argmax())
            out_ids.append(nxt)
            step_ids.append(nxt)
            if nxt == 2:
                break
        return out_ids

    def text_of(self, ids) -> str:
        return self.tok.decode([i for i in ids if i > 3], skip_special_tokens=False).strip()

    def see(self, img, task: str = "caption", max_tokens: int = 120) -> str:
        return self.text_of(self.generate(img, task, max_tokens))

    def objects(self, img, max_tokens: int = 200) -> list[tuple[str, tuple[int, int, int, int]]]:
        """[(label, (x0, y0, x1, y1) in the image's own pixels)] from the <OD> task's location tokens."""
        w, h = img.size
        text = self.tok.decode([i for i in self.generate(img, "objects", max_tokens) if i > 3], skip_special_tokens=False)
        found = []
        for m in re.finditer(r"([^<]+?)((?:<loc_\d+>){4})", text):
            label = m.group(1).strip()
            x0, y0, x1, y1 = (int(v) for v in re.findall(r"<loc_(\d+)>", m.group(2)))
            found.append((label, (int(x0 / 1000 * w), int(y0 / 1000 * h), int(x1 / 1000 * w), int(y1 / 1000 * h))))
        return found


def _repeats(ids: list[int]) -> set[int]:
    """Tokens that would complete a repeated 3-gram (no_repeat_ngram_size=3, as the model was tuned with)."""
    if len(ids) < 3:
        return set()
    tail = tuple(ids[-2:])
    return {ids[i + 2] for i in range(len(ids) - 2) if tuple(ids[i:i + 2]) == tail}


def get(auto_download: bool = True, progress=None) -> Looker:
    """The shared Looker, loaded on first use (downloads the weights when allowed)."""
    global _model
    with _lock:
        if _model is not None:
            return _model
        why = runtime_missing()
        if why:
            raise LookerUnavailable(why)
        if not installed():
            if not auto_download:
                raise LookerUnavailable("the local vision model is not downloaded yet — run: hexiel --get-vision")
            try:
                download(progress)
            except Exception as exc:  # noqa: BLE001 - offline, disk full, …
                raise LookerUnavailable(f"could not download the local vision model: {exc}") from exc
        _model = Looker(model_dir())
        return _model
