"""crow web launcher — starts FastAPI server with graceful shutdown.

Usage:
    crow                    # launch web UI on http://127.0.0.1:8000
    crow --port 3000        # custom port
    crow --host 0.0.0.0    # bind address

Ctrl+C kills server + cron scheduler. No zombie processes.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

# Ensure project root is on PYTHONPATH so `import app` works
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load .env file (silently skip if missing — env vars may be set externally)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

logger = logging.getLogger("crow_agent.launcher")


def _check_deps() -> None:
    """Verify all web deps are installed."""
    for pkg in ("uvicorn", "fastapi", "jinja2"):
        try:
            __import__(pkg)
        except ImportError:
            logger.error("Error: %s not installed. Activate your venv and run: pip install %s", pkg, pkg)
            sys.exit(1)


def main() -> None:
    _check_deps()

    # Validate config before starting server
    from .config_check import check_or_exit
    check_or_exit()

    parser = argparse.ArgumentParser(prog="crow", description="Launch Crow Agent web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn
    import subprocess

    # Import app after arg parse so --help is fast
    from app import app

    # Kill any existing process on the target port to avoid bind error
    try:
        pid = subprocess.check_output(
            ["lsof", "-ti", f":{args.port}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if pid:
            os.kill(int(pid), signal.SIGTERM)
            logger.warning("Killed stale process PID=%s on port %s", pid, args.port)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        pass

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    # Prevent double SIGINT — uvicorn handles it, but this ensures clean exit
    # No extra signal handling needed; uvicorn.Server.run() handles SIGINT/SIGTERM
    # and triggers FastAPI lifespan shutdown → cron.stop() → all tasks cancelled

    logger.info("🐦‍⬛ Crow Agent → http://%s:%s", args.host, args.port)
    logger.info("   Ctrl+C to quit")

    server.run()


if __name__ == "__main__":
    main()
