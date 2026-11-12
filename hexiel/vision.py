"""Vision for text-only models: does a model see images, and if not, replace each image in the history with
what a vision model sees in it (digest.describe), once, before the request goes out."""

from __future__ import annotations

import re

# Model families that take images. Matched against the model id, lowercased; a profile's `vision = "yes"|"no"`
# always wins over this guess.
_SEES = re.compile(
    r"claude|gpt-4o|gpt-4\.1|gpt-5|o3|o4|gemini|gemma3|gemma-3|gemma4|gemma-4|llava|bakllava|moondream|"
    r"minicpm-v|qwen.*vl|qwen2\.5vl|qwen3-vl|llama3\.2-vision|llama-3\.2-.*vision|llama4|pixtral|"
    r"mistral-small3\.[12]|mistral-medium|granite3\.2-vision|kimi-k2\.5|glm-4\.5v|glm-4v|internvl|vision")


# Families known to be text-only: their images are described. A model in neither list keeps its images (the
# old behaviour) — set `vision = "no"` on its profile to have them described instead.
_BLIND = re.compile(
    r"qwen\d?(\.\d)?-?coder|qwen3(?!.*vl)|qwq|deepseek|gpt-oss|glm-4(?:\.\d)?(?!.*v)|kimi-k2(?!\.5)|minimax|devstral|"
    r"codestral|codellama|starcoder|phi[34]|mistral-nemo|llama3\.[0-3](?!.*vision)|llama-3\.[0-3](?!.*vision)|"
    r"granite(?!.*vision)|nemotron|command-r|olmo|smollm|tinyllama|openhermes|dolphin|o1-mini|o3-mini|gpt-3\.5")


def sees_images(mc) -> bool:
    said = (getattr(mc, "vision", "") or "").strip().lower()
    if said in ("yes", "true", "1", "on"):
        return True
    if said in ("no", "false", "0", "off"):
        return False
    model = (getattr(mc, "model", "") or "").lower()
    if _SEES.search(model):
        return True
    return not _BLIND.search(model)


def describe_history(session) -> int:
    """Turn every image still in the history into text when the current model can't see images.
    Returns how many were described. A failure leaves a clear placeholder instead — never a dropped image
    the model silently ignores, never a failed turn."""
    if sees_images(session.model_config):
        return 0
    from . import digest
    question = session.__dict__.get("_turn_request", "") or ""
    done = 0
    for msg in session.messages:
        images = msg.get("images")
        if not images:
            continue
        notes = []
        for n, image in enumerate(images, 1):
            label = f"image {n}" if len(images) > 1 else "image"
            if session._stop.is_set():
                notes.append(f"[{label} not described: stopped]")
                continue
            try:
                session.on_event("note", f"👁 {session.model_config.name} can't see images — asking a vision model to describe the {label}")
                text = digest.describe(session, {k: image[k] for k in ("media_type", "data") if k in image}, question)
                notes.append(f"[{label}, described by a vision model because {session.model_config.name} is text-only]\n{text}")
                done += 1
            except Exception as exc:  # noqa: BLE001 - say why, carry on
                notes.append(f"[{label} attached but not visible to this model: {str(exc)[:200]}]")
        msg.pop("images", None)
        msg["content"] = ((msg.get("content") or "") + "\n\n" + "\n\n".join(notes)).strip()
    if done:
        session.save()
    return done


def ocr(data: bytes, timeout: float = 30.0) -> str:
    """The no-model path: the text in an image via the tesseract CLI, or "" when tesseract is missing or
    finds nothing. Image bytes go in on stdin, text comes out on stdout — no temp files."""
    import shutil
    import subprocess  # nosec B404 - a fixed argv, no shell
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "stdin", "stdout"], input=data, capture_output=True,  # nosec B603
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = out.stdout.decode("utf-8", "replace")
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1])).strip()
