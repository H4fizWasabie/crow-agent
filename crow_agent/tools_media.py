"""Media tools: image generation, OCR, TTS."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT
from .tools_common import parse_page_spec


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register media tools."""

    @registry.register(
        description="Generate an image from a text prompt using OpenRouter Seedream 4.5 ($0.004/image). Falls back to HuggingFace FLUX if unavailable."
    )
    def generate_image(prompt: str, model: str = "bytedance-seed/seedream-4.5", negative_prompt: str = "") -> str:
        import base64
        import httpx
        import time

        default_dir = Path.home() / ".crow_agent" / "generated_images"
        save_dir = Path(os.environ.get("CROW_AGENT_IMAGE_DIR", str(default_dir)))
        save_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)[:60].strip()
        fname = f"{ts}_{safe}.png"
        fpath = save_dir / fname

        # ── Primary: OpenRouter Seedream 4.5 ($0.004/image) ──
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            try:
                resp = httpx.post(
                    "https://openrouter.ai/api/v1/images/generations",
                    headers={
                        "Authorization": f"Bearer {or_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "prompt": prompt,
                        "n": 1,
                        "size": "1024x1024",
                    },
                    timeout=120,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    data = body.get("data", [{}])
                    img = data[0] if data else {}
                    b64 = img.get("b64_json", "")
                    url = img.get("url", "")
                    if b64:
                        fpath.write_bytes(base64.b64decode(b64))
                        return f"Image saved: {fpath} (Seedream 4.5)"
                    elif url:
                        img_resp = httpx.get(url, timeout=30)
                        fpath.write_bytes(img_resp.content)
                        return f"Image saved: {fpath} (Seedream 4.5)"
                # 4xx/5xx → fall through to HF
                logger.info("OpenRouter image failed (%d), trying HF", resp.status_code)
            except Exception:
                logger.info("OpenRouter image failed, trying HF", exc_info=True)

        # ── Fallback: HuggingFace FLUX ──
        hf_key = os.environ.get("HF_API_KEY", "")
        if not hf_key:
            return "No image API key. Set OPENROUTER_API_KEY or HF_API_KEY."
        try:
            payload: dict[str, Any] = {"inputs": prompt}
            if negative_prompt:
                payload["parameters"] = {"negative_prompt": negative_prompt}
            resp = httpx.post(
                f"https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                headers={"Authorization": f"Bearer {hf_key}", "Accept": "image/png"},
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            fpath.write_bytes(resp.content)
            return f"Image saved: {fpath} (FLUX.1-schnell via HF)"
        except Exception as exc:
            return f"Image generation error: {exc}"

    @registry.register(
        description="Extract text from an image or PDF using OCR. Supports JPG, PNG, PDF. Falls back to Gemma 4 31B vision if docTR unavailable."
    )
    def ocr_document(file_path: str, pages: str = "") -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        result = _ocr_doctr(path, pages)
        if result and not result.startswith("docTR not installed") and not result.startswith("OCR error") and not result.startswith("Error"):
            return result

        if result:
            logger.info("docTR failed (%s), falling back to Gemma vision OCR", result[:80])
        return _ocr_gemma_vision(path)

    @registry.register(
        description="Transcribe speech from an audio file (MP3, WAV, OGG, M4A) to text using faster-whisper."
    )
    def hear(file_path: str, language: str = "en") -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return "faster-whisper not installed. Run: pip install faster-whisper"

        try:
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, info = model.transcribe(str(path), language=language or None)
            text = " ".join(s.text for s in segments)
            lang = info.language if info else "?"
            return f"[{lang}] {text}" if text.strip() else "(silent)"
        except Exception as exc:
            return f"Hear error: {exc}"

    @registry.register(
        name="see_image",
        description="Analyse an image using OpenRouter vision (Gemma 4 31B). Provide an image path and a prompt/question about it. Good for reading screenshots, invoices, dashboards."
    )
    def see_image(image_path: str, prompt: str = "Describe this image in detail.") -> str:
        import base64
        import httpx
        from pathlib import Path

        path = Path(image_path)
        if not path.exists():
            return f"File not found: {image_path}"
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
            return f"Unsupported image format: {path.suffix}. Supported: JPG, PNG, BMP, WEBP."

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return "OPENROUTER_API_KEY not set. Vision unavailable."

        ext = path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".bmp": "image/bmp",
                    ".webp": "image/webp"}
        mime = mime_map.get(ext, "image/png")
        data = base64.b64encode(path.read_bytes()).decode()

        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "google/gemma-4-31b-it",
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                    ]}],
                    "max_tokens": 4096,
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            return f"Vision error: {exc}"

    @registry.register(
        description="Convert text to speech using edge-tts and save as audio file (.ogg). Default voice: English male (Andrew)."
    )
    def say(text: str, voice: str = "en-US-AndrewNeural") -> str:
        if not text.strip():
            return "Nothing to say."
        import asyncio
        import edge_tts
        import time

        audio_dir = Path(os.environ.get("CROW_AGENT_AUDIO_DIR", str(PROJECT_ROOT / "audio")))
        audio_dir.mkdir(parents=True, exist_ok=True)
        path = audio_dir / f"tts_{int(time.time())}.ogg"

        async def _synth():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))

        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(lambda: __import__("asyncio").new_event_loop().run_until_complete(_synth())).result(timeout=30)
            return f"Audio saved: {path}"
        except Exception as exc:
            return f"TTS error: {exc}"


# ── OCR helpers ──

def _ocr_doctr(path: Path, pages: str = "") -> str:
    """Primary OCR: docTR (PyTorch-based, local)."""
    try:
        from doctr.io import DocumentFile
        from doctr.models import ocr_predictor
    except ImportError:
        return "docTR not installed. Run: pip install crow-agent[doctr]"

    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            doc = DocumentFile.from_pdf(str(path))
        elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"):
            doc = DocumentFile.from_images(str(path))
        else:
            return f"Unsupported file type: {ext}. Supported: JPG, PNG, PDF."
    except Exception as exc:
        return f"Error loading file: {exc}"

    if pages and ext == ".pdf":
        try:
            indices = parse_page_spec(pages, len(doc))
            doc = [doc[i] for i in indices]
        except ValueError as exc:
            return f"Invalid page spec '{pages}': {exc}"

    try:
        model = ocr_predictor(pretrained=True)
        result = model(doc)
        return result.render()
    except Exception as exc:
        return f"OCR error: {exc}"


def _ocr_gemma_vision(path: Path) -> str:
    """Fallback OCR: Gemma 4 31B vision via OpenRouter (free, no local deps)."""
    import base64
    import httpx

    try:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return "OPENROUTER_API_KEY not set"

        ext = path.suffix.lower()
        mime_map = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".bmp": "image/bmp", ".tiff": "image/tiff",
                    ".tif": "image/tiff"}
        mime = mime_map.get(ext, "image/png")

        data = base64.b64encode(path.read_bytes()).decode()

        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "google/gemma-4-31b-it:free",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Extract all text from this image. Return ONLY the text, no commentary."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ]}],
                "max_tokens": 2000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"Gemma vision OCR error: {exc}"
