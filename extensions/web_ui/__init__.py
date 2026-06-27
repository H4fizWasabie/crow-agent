"""Web UI Extension — optional n8n-style dashboard for Crow.

Runs as a separate FastAPI app. Crow core (agent, telegram, cron)
operates independently — this extension is purely visual.

Start: python extensions/web_ui/server.py
Or: uvicorn extensions.web_ui.server:app --host 0.0.0.0 --port 8000
"""

from .server import app, start

__all__ = ['app', 'start']
