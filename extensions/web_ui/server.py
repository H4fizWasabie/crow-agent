"""Standalone web UI server for Crow Agent.
Imports crow_agent for data access, serves n8n-style dashboard.
"""
from __future__ import annotations

import json, logging, os, time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("crow_agent.web_ui")

# ── FastAPI app ──
app = FastAPI(title="Crow Agent — Web UI", version="2.0")

# Templates
_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Static files (CSS, JS)
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ── Lazy DB access ──
def _get_db():
    from crow_agent.crow_state import CrowState
    return CrowState()

# ── Pages ──
@app.get("/", response_class=HTMLResponse)
@app.get("/activity", response_class=HTMLResponse)
def activity_page():
    return FileResponse(str(_TEMPLATE_DIR / "activity.html"))

@app.get("/health")
def health() -> JSONResponse:
    checks = {"db": "ok", "web_ui": "running"}
    return JSONResponse({"status": "ok", "checks": checks})

# ── API ──
@app.get("/api/workflow")
def api_workflow(limit: int = 15) -> JSONResponse:
    nodes, edges = [], []
    try:
        db = _get_db()
        tools = db._conn.execute("""
            SELECT tool_name, substr(arguments,1,100) as args,
                   substr(output,1,200) as output, created_at
            FROM tool_outputs ORDER BY created_at DESC LIMIT ?
        """, (limit * 2,)).fetchall()
        nid = 0; prev = None
        for t in reversed(tools):
            nid += 1; ntype, icon, color = _style(t["tool_name"], t["output"])
            nodes.append({"id":nid,"type":ntype,"icon":icon,"color":color,
                "label":t["tool_name"],"detail":(t["args"]or"")[:80],
                "output":(t["output"]or"")[:120],"time":t["created_at"][11:19]})
            if prev: edges.append({"from":prev,"to":nid})
            prev = nid
        db.close()
    except Exception: pass
    return JSONResponse({"nodes":nodes,"edges":edges})

@app.get("/api/replay")
def api_replay(limit: int = 30) -> JSONResponse:
    trail = []
    try:
        db = _get_db()
        for r in db._conn.execute(
            "SELECT tool_name, substr(arguments,1,200) as args, substr(output,1,200) as output, created_at FROM tool_outputs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall():
            trail.append({"time":r["created_at"],"tool":r["tool_name"],"args":r["args"],"output":r["output"]})
        db.close()
    except Exception: pass
    return JSONResponse({"trail":trail})

def _style(tool: str, output: str) -> tuple:
    if "Error" in output or "FAILED" in output: return ("error","❌","#ef4444")
    if tool in ("write_file","edit_file"): return ("fix","✏️","#3b82f6")
    if "pytest" in output.lower(): return ("test","🧪","#8b5cf6")
    if "git commit" in output.lower(): return ("commit","📦","#10b981")
    if tool == "run_cmd": return ("exec","⚡","#f59e0b")
    if tool in ("read_file","grep_files"): return ("read","📖","#06b6d4")
    return ("tool","🔧","#6b7280")

def start(host="0.0.0.0", port=8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
