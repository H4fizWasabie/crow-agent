"""YouTube transcription extension for Crow Agent.

Captions-first (free, fast), whisper fallback when captions unavailable.
Saves transcript to memory vault as markdown.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from crow_agent.toolsets import ToolRegistry

logger = logging.getLogger(__name__)

_YT_ID_RE = re.compile(r"(?:v=|/)([a-zA-Z0-9_-]{11})")


def _extract_video_id(url: str) -> str | None:
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:100]


def register_tools(registry: ToolRegistry) -> None:
    @registry.register(
        description="Transcribe a YouTube video. Fetches captions (free, no API key). Saves transcript to memory vault/youtube/ as markdown. URL required."
    )
    def youtube_transcribe(url: str) -> str:
        return _youtube_transcribe(url)


def _youtube_transcribe(url: str) -> str:
    video_id = _extract_video_id(url)
    if not video_id:
        return "Error: Could not extract YouTube video ID from URL. Use full URL like https://youtube.com/watch?v=..."

    # Try captions first (free, fast, no API key)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        # IMPORTANT: YouTubeTranscriptApi v1.2.4 requires INSTANCE creation before calling methods.
        # DO NOT call fetch()/list() as class methods on YouTubeTranscriptApi directly.
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        title = "Untitled"
        try:
            for t in api.list(video_id):
                title = t.title
                break
        except Exception:
            pass

        # Build markdown
        lines = [f"# {title}", f"\nSource: https://youtube.com/watch?v={video_id}\n"]
        for entry in transcript:
            lines.append(entry.text)

        return _save_and_report(title, video_id, "\n".join(lines))

    except Exception as captions_err:
        msg = str(captions_err).lower()
        if "disabled" in msg or "no transcript" in msg:
            # Fallback to whisper (no captions available)
            try:
                return _whisper_fallback(video_id, url)
            except Exception as whisper_err:
                return f"Error: No captions available and whisper fallback failed: {whisper_err}"
        return f"Error: {captions_err}"


def _whisper_fallback(video_id: str, url: str) -> str:
    """Download audio with yt-dlp, transcribe with faster-whisper.

    IMPORTANT: This function is a fallback when YouTube captions are disabled.
    It requires `yt-dlp` and `faster-whisper` to be installed.
    """
    import tempfile
    import subprocess

    # ---- Guard: check yt-dlp is available ----
    yt_dlp_available = False
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=10)
        yt_dlp_available = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if not yt_dlp_available:
        return (
            "Error: yt-dlp is not installed. Cannot download audio for whisper fallback.\n"
            "Install with: pip install yt-dlp"
        )

    # ---- Guard: check faster-whisper is available ----
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return (
            "Error: faster-whisper is not installed. Cannot transcribe audio.\n"
            "Install with: pip install faster-whisper"
        )

    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(tmp) / "audio.mp3"

        # ---- Download audio with yt-dlp ----
        # GUARD: check return code — do NOT silently continue on failure
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "--extract-audio", "--audio-format", "mp3",
             "-o", str(audio_path), url],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            return (
                f"Error: yt-dlp download failed (exit code {result.returncode}).\n"
                f"Stderr: {stderr}"
            )

        # ---- Transcribe with faster-whisper ----
        model = WhisperModel("tiny", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path))

        # ---- Get title ----
        # IMPORTANT: _whisper_fallback has its OWN local scope.
        # YouTubeTranscriptApi must be INSTANTIATED here — do NOT use it as
        # an unbound class reference. (See _youtube_transcribe for the same rule.)
        title = "Untitled"
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            # MUST instantiate before calling list() — instance method, not classmethod
            whisper_api = YouTubeTranscriptApi()
            for t in whisper_api.list(video_id):
                title = t.title
                break
        except Exception:
            pass

        # ---- Build markdown with fallback disclaimer ----
        lines = [
            f"# {title}",
            f"\nSource: {url}\n",
            "*Transcribed via Whisper (no captions).*",
            "",
        ]
        for seg in segments:
            lines.append(seg.text)

        return _save_and_report(title, video_id, "\n".join(lines))


def _save_and_report(title: str, video_id: str, content: str) -> str:
    vault_dir = Path(os.environ.get("MEMORY_VAULT_DIR", "memory vault")) / "youtube"
    vault_dir.mkdir(parents=True, exist_ok=True)

    filename = _sanitize_filename(f"{video_id}_{title}") + ".md"
    path = vault_dir / filename
    path.write_text(content, encoding="utf-8")

    return f"✅ Transcribed: **{title}**\n- Saved: `{path}`\n- Length: {len(content)} chars\n- Source: https://youtube.com/watch?v={video_id}"
