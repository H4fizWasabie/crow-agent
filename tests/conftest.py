"""Shared fixtures for Crow Agent tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest

from crow_agent.crow_state import CrowState


@pytest.fixture
def tmp_home() -> Generator[Path, None, None]:
    """Temporary home directory for config/secrets isolation."""
    with tempfile.TemporaryDirectory() as td:
        old_home = Path.home()
        tmp = Path(td)
        # Don't actually override — just give caller a scratch dir
        yield tmp


@pytest.fixture
def db() -> Generator[CrowState, None, None]:
    """In-memory CrowState."""
    store = CrowState(db_path=":memory:")
    store.create_session("test_session")
    yield store
    store.close()



@pytest.fixture
def sample_history() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "hello", "prompt_tokens": 0, "completion_tokens": 0},
        {"role": "assistant", "content": "hi there", "prompt_tokens": 10, "completion_tokens": 5},
        {"role": "user", "content": "what's the weather?", "prompt_tokens": 0, "completion_tokens": 0},
        {"role": "assistant", "content": "sunny", "prompt_tokens": 15, "completion_tokens": 3},
    ]
