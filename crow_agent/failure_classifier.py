"""Failure classifier — categorizes agent failures for self-improvement.

Regex-based: classifies error strings into 7 categories. Enables
self-improvement routing (e.g. BAD_DECOMPOSITION → reflect, MISSING_SKILL → learn).
"""

from __future__ import annotations

import re
from enum import Enum


class FailureCategory(Enum):
    MISSING_SKILL = "missing_skill"
    MISSING_TOOL = "missing_tool"
    MISSING_MEMORY = "missing_memory"
    BAD_DECOMPOSITION = "bad_decomposition"
    BAD_VERIFICATION = "bad_verification"
    CONTEXT_OVERLOAD = "context_overload"
    UNSAFE = "unsafe"


_PATTERNS: list[tuple[re.Pattern, FailureCategory]] = [
    (re.compile(r"dangerous|blocked|permission denied|not allowed|forbidden", re.I),
     FailureCategory.UNSAFE),
    (re.compile(r"budget|truncat|round limit|context.*limit|context.*exceed", re.I),
     FailureCategory.CONTEXT_OVERLOAD),
    (re.compile(r"consecutive.*fail|worker.*fail|spawn.*fail", re.I),
     FailureCategory.BAD_DECOMPOSITION),
    (re.compile(r"assertionerror|assert.*fail|test.*fail", re.I),
     FailureCategory.BAD_VERIFICATION),
    (re.compile(r"modulenotfounderror|no module named|importerror", re.I),
     FailureCategory.MISSING_TOOL),
    (re.compile(r"not found|no such file|keyerror|unknown.*refer", re.I),
     FailureCategory.MISSING_MEMORY),
]


def classify_failure(error: str, context: str = "") -> FailureCategory:
    """Classify a failure into one of 7 categories based on error text."""
    combined = f"{error} {context}"
    for pattern, category in _PATTERNS:
        if pattern.search(combined):
            return category
    return FailureCategory.MISSING_SKILL
