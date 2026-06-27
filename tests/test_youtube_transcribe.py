"""Tests for youtube_transcribe extension.

Covers:
- Captions path (primary)
- Whisper fallback path (when captions disabled)
- Invalid URL
- Subprocess return code guard
- Dependency guards
- Instance method vs classmethod enforcement
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from crow_agent.toolsets import ToolRegistry

# ---------------------------------------------------------------------------
# Module-level mock: faster_whisper is not installed in CI/dev, but the code
# imports it lazily inside _whisper_fallback().  We make it importable by
# pre-populating sys.modules with a stub.  This also lets
#     patch("faster_whisper.WhisperModel")
# resolve its target without raising ImportError.
# ---------------------------------------------------------------------------
if "faster_whisper" not in sys.modules:
    sys.modules["faster_whisper"] = MagicMock()


@pytest.fixture
def yt_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "memory vault"))

    import importlib
    from crow_agent.paths import PROJECT_ROOT

    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    for key in list(sys.modules.keys()):
        if key.startswith("extensions.youtube_transcribe"):
            del sys.modules[key]

    mod = importlib.import_module("extensions.youtube_transcribe")
    reg = ToolRegistry()
    mod.register_tools(reg)
    yield reg


# ===============================================================
#  Captions path (primary)
# ===============================================================


def test_youtube_transcribe_captions(yt_registry, tmp_path):
    """Primary captions path works end-to-end."""
    from youtube_transcript_api._transcripts import FetchedTranscriptSnippet

    mock_transcript = [
        FetchedTranscriptSnippet(text="Hello world", start=0.0, duration=2.0),
        FetchedTranscriptSnippet(text="This is a test", start=2.0, duration=3.0),
    ]

    with patch(
        "youtube_transcript_api.YouTubeTranscriptApi.fetch",
        return_value=mock_transcript,
    ):
        mock_list = MagicMock()
        mock_list.video_id = "dQw4w9WgXcQ"
        mock_list.title = "Test Video Title"
        mock_list.__iter__ = lambda s: iter([s])

        with patch(
            "youtube_transcript_api.YouTubeTranscriptApi.list",
            return_value=mock_list,
        ):
            result = yt_registry.execute(
                "youtube_transcribe",
                {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
            )

    assert "transcribed" in result.lower() or "saved" in result.lower()
    vault = tmp_path / "memory vault" / "youtube"
    assert vault.exists()
    md_files = list(vault.glob("*.md"))
    assert len(md_files) >= 1


def test_youtube_transcribe_captions_instantiation(yt_registry):
    """YouTubeTranscriptApi is INSTANTIATED before calling fetch()/list()."""
    mock_transcript = [{"text": "test", "start": 0.0, "duration": 1.0}]

    with patch(
        "youtube_transcript_api.YouTubeTranscriptApi"
    ) as MockApi:
        instance = MockApi.return_value
        instance.fetch.return_value = mock_transcript
        mock_list_entry = MagicMock()
        mock_list_entry.title = "Title"
        instance.list.return_value = [mock_list_entry]

        yt_registry.execute(
            "youtube_transcribe",
            {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
        )

        # PROOF: YouTubeTranscriptApi() is called (instance created), then
        # methods called on that instance — NOT as classmethods.
        MockApi.assert_called_once_with()  # instance created
        instance.fetch.assert_called_once()
        instance.list.assert_called_once()


# ===============================================================
#  Whisper fallback path
# ===============================================================


@pytest.fixture
def _mock_whisper_model():
    """Set up a mock WhisperModel that returns dummy segments."""
    with patch("faster_whisper.WhisperModel") as MockModel:
        model_instance = MockModel.return_value
        seg = MagicMock(text="hello from whisper")
        model_instance.transcribe.return_value = ([seg], None)
        yield MockModel


def test_youtube_transcribe_whisper_fallback(yt_registry, tmp_path, _mock_whisper_model):
    """Whisper fallback activates when captions return 'disabled' error."""
    with patch(
        "youtube_transcript_api.YouTubeTranscriptApi.fetch",
        side_effect=Exception("TranscriptsDisabled"),
    ):
        mock_list_entry = MagicMock()
        mock_list_entry.title = "Whisper Fallback Title"

        with patch(
            "youtube_transcript_api.YouTubeTranscriptApi.list",
            return_value=[mock_list_entry],
        ):
            with patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stderr=b""),
            ) as mock_run:
                result = yt_registry.execute(
                    "youtube_transcribe",
                    {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
                )

    assert "transcribed" in result.lower() or "saved" in result.lower()
    vault = tmp_path / "memory vault" / "youtube"
    assert vault.exists()

    # Verify yt-dlp was called
    yt_dlp_call = any(
        "yt-dlp" in c.args[0] if hasattr(c, "args") else False
        for c in mock_run.call_args_list
    )
    assert yt_dlp_call, "yt-dlp should have been invoked during whisper fallback"


def test_whisper_fallback_api_instantiation(yt_registry, _mock_whisper_model):
    """YouTubeTranscriptApi is INSTANTIATED in whisper fallback (not classmethod)."""
    with patch(
        "youtube_transcript_api.YouTubeTranscriptApi.fetch",
        side_effect=Exception("TranscriptsDisabled"),
    ):
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stderr=b""),
        ):
            # Patch YouTubeTranscriptApi itself to track instantiation
            with patch(
                "youtube_transcript_api.YouTubeTranscriptApi"
            ) as MockApi:
                instance = MockApi.return_value
                mock_list_entry = MagicMock()
                mock_list_entry.title = "Title"
                instance.list.return_value = [mock_list_entry]

                yt_registry.execute(
                    "youtube_transcribe",
                    {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
                )

                # PROOF: YouTubeTranscriptApi() is called (instance created)
                # in whisper fallback — NOT used as unbound class.
                MockApi.assert_called()  # called at least once


# ===============================================================
#  Subprocess return code guard
# ===============================================================


def test_whisper_fallback_ytdlp_failure(yt_registry, _mock_whisper_model):
    """Whisper fallback returns error when yt-dlp fails (non-zero exit)."""
    with patch(
        "youtube_transcript_api.YouTubeTranscriptApi.fetch",
        side_effect=Exception("TranscriptsDisabled"),
    ):
        with patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1, stderr=b"ERROR: video not found"
            ),
        ):
            result = yt_registry.execute(
                "youtube_transcribe",
                {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
            )

    assert "error" in result.lower()
    assert "yt-dlp" in result.lower() or "download failed" in result.lower()


# ===============================================================
#  Dependency guards
# ===============================================================


def test_whisper_fallback_no_whisper(yt_registry):
    """Returns helpful error when faster-whisper is not installed.

    We simulate this by temporarily removing the module from sys.modules
    so the lazy import inside _whisper_fallback raises ImportError.
    """
    saved = sys.modules.pop("faster_whisper", None)
    try:
        with patch(
            "youtube_transcript_api.YouTubeTranscriptApi.fetch",
            side_effect=Exception("TranscriptsDisabled"),
        ):
            with patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stderr=b""),
            ):
                result = yt_registry.execute(
                    "youtube_transcribe",
                    {"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
                )

        assert "error" in result.lower()
        assert "faster-whisper" in result.lower()
    finally:
        if saved is not None:
            sys.modules["faster_whisper"] = saved


# ===============================================================
#  Invalid URL
# ===============================================================


def test_youtube_transcribe_invalid_url(yt_registry):
    result = yt_registry.execute("youtube_transcribe", {"url": "not-a-url"})
    assert "error" in result.lower() or "invalid" in result.lower()
