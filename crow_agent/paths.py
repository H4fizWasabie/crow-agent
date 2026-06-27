"""Project path resolution.

All context files (MEMORY.md, USER.md, SOUL.md) and agent-generated
content resolve relative to the project root so that CWD doesn't matter.
"""

from pathlib import Path

# crow_agent/paths.py → crow_agent/ → project root
PROJECT_ROOT: Path = Path(__file__).parent.parent
