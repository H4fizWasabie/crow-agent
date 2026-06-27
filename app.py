"""FastAPI web UI for Crow Agent.

Four tabs: Chat, Sessions, Skills, Cron.
HTMX for dynamic updates. SSE for streaming chat responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from crow_agent.run_agent import AIAgent, Trigger, TriggerSource
from crow_agent.agent_profiles import load_all_profiles, run_child_task
from crow_agent.toolsets import ToolRegistry
from crow_agent.model_tools import register_builtins
from crow_agent.skills_system import scan_skills_dirs
from crow_agent.crow_state import CrowState
from crow_agent.cron_engine import CronEngine
from crow_agent.provider_manager import ProviderEntry, ProviderManager
from crow_agent.reminder_engine import ReminderEngine

# PostgreSQL connection for price search dashboard
# Set CROW_PRICES_DB_URL in .env to enable. Fallback None = dashboard features disabled.
PG_DSN: str | None = os.environ.get("CROW_PRICES_DB_URL") or None

# Web auth token. If set, all web routes require Authorization: Bearer <token>.
# If unset, auth is disabled (safe when bound to localhost).
_WEB_TOKEN: str | None = os.environ.get("CROW_WEB_TOKEN") or None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("crow_agent.web")

# --- shared state ---

_tools = ToolRegistry()
_profiles = load_all_profiles()
_task_db = CrowState()
_reminder: ReminderEngine | None = None


def _retrieve_output(output_id: str) -> str:
    """Retrieve a full tool output by ID from the agent's DB."""
    db = _sessions_db()
    try:
        result = db.get_tool_output(output_id)
        return result or f"Output {output_id} not found (may have expired)"
    finally:
        db.close()


def _spawn_child(role: str, task: str) -> str:
    """Spawn a child agent using a team profile."""
    profile = _profiles.get(role)
    if not profile:
        return f"Error: unknown agent role '{role}'. Available: {', '.join(_profiles)}"
    from crow_agent.providers import resolve_provider
    provider_name = profile.model or "opencode-go"
    try:
        provider = resolve_provider(provider_name, provider_manager=_pm)
    except Exception:
        # Profile specific provider not found — fall back to opencode-zen
        try:
            provider = resolve_provider("opencode-zen", provider_manager=_pm)
        except Exception as exc:
            return f"Error: no provider for '{role}' (tried '{provider_name}' then opencode-zen): {exc}"
    return run_child_task(profile, task, provider, _tools)


def _delegate_task(prompt: str, chat_id: int = 0) -> str:
    """Queue a task for autonomous background execution."""
    from crow_agent.task_registry import enqueue
    task_id = enqueue(prompt=prompt, chat_id=chat_id or 0)
    return f"✅ Task _{task_id}_ queued. Result will appear when done."


register_builtins(_tools, spawn_fn=_spawn_child, retrieve_fn=_retrieve_output, task_db=_task_db, delegate_fn=_delegate_task)
_skills = scan_skills_dirs()
_cron = CronEngine()
_pm = ProviderManager()
_heartbeat: Any | None = None  # HeartbeatEngine, initialized in lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage cron + Telegram bot lifecycle."""
    # Seed provider config from .env on first run
    try:
        _pm.seed_from_env()
    except Exception:
        logger.warning("Failed to seed provider config from .env — check your environment", exc_info=True)

    # --- cron ---
    _cron.start()
    logger.info("Cron scheduler started")

    # Register cron runner + delivery (captures _telegram_bot / _tg_chat_id as closures)
    active_provider = _pm.active or "opencode-go"
    _cron.set_runner(CronEngine.make_runner(provider_name=active_provider))

    async def _cron_deliver(job_id: str, result: str, error: str | None) -> None:
        """Deliver cron job result to Crow Log channel."""
        if error:
            text = f"🕐 Cron job _{job_id}_ disabled:\n\n{error}"
        else:
            text = f"🕐 Cron job _{job_id}_ done:\n\n{result[:2000]}"
        if _telegram_bot:
            from crow_agent.telegram_bot import send_to_crow_log
            await send_to_crow_log(_telegram_bot._app.bot, text)

    _cron.set_notify(_cron_deliver)

    # --- shared DB for secretary ---
    global _reminder

    # --- Telegram bot (optional) ---
    _telegram_bot = None
    _tg_chat_id = None
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    allowed = os.environ.get("TELEGRAM_ALLOWED_IDS", "").strip()
    if token and allowed:
        from crow_agent.telegram_bot import TelegramBot

        ids = {int(x.strip()) for x in allowed.split(",") if x.strip()}
        if ids:
            _tg_chat_id = next(iter(ids))
            _telegram_bot = TelegramBot(token, _agent, ids, db=_task_db)
            await _telegram_bot.start()
    elif token and not allowed:
        logger.warning("TELEGRAM_TOKEN set but TELEGRAM_ALLOWED_IDS is empty — bot not started")

    # --- Reminder engine (async task nag loop) ---
    _reminder = ReminderEngine(
        db=_task_db,
        send_fn=_telegram_bot.send_reminder if _telegram_bot else None,
        chat_id=_tg_chat_id,
    )
    _reminder.start()

    # --- Heartbeat (autonomous idle awareness) ---
    global _heartbeat
    from crow_agent.heartbeat_engine import HeartbeatEngine
    from crow_agent.providers import resolve_provider

    # ponytail: try deepseek-free first (not MiMo — won't do background work), fallback to any
    _hb_provider = None
    for _hb_name in ("opencode-zen-1", "opencode-zen-2", "opencode-go", "openrouter"):
        try:
            _hb_provider = resolve_provider(_hb_name, provider_manager=_pm)
            logger.info("Heartbeat using provider: %s", _hb_name)
            break
        except Exception:
            pass
    if not _hb_provider:
        logger.warning("Heartbeat: no provider available — autonomous features disabled")

    # Crow Log callback for autonomous activity feed
    _crow_log_cb = None
    if _telegram_bot:
        from crow_agent.telegram_bot import send_to_crow_log as _scl
        _crow_log_cb = lambda t: _scl(_telegram_bot._app.bot, t)

    _heartbeat = HeartbeatEngine(
        db=_task_db,
        cron_engine=_cron,
        send_fn=(lambda t: _telegram_bot.send_message(_tg_chat_id, t)) if _telegram_bot and _tg_chat_id else None,
        tool_registry=_tools,
        provider=_hb_provider,
        project_root=Path(__file__).parent,
        chat_id=_tg_chat_id or 0,
        crow_log_fn=_crow_log_cb,
    )
    _heartbeat.start()

    yield

    await _heartbeat.stop()

    await _reminder.stop()
    if _telegram_bot:
        await _telegram_bot.stop()
    await _cron.stop()
    logger.info("Reminder engine + Telegram bot + cron stopped")


# Resolve static/templates relative to this file so `crow` works from any cwd
_HERE = Path(__file__).parent

app = FastAPI(title="Crow Agent", lifespan=lifespan)

# Optional bearer-token auth middleware
if _WEB_TOKEN:

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next: Any) -> Response:
        # Skip auth for health endpoint
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_WEB_TOKEN}":
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))

def _agent(session_id: str = "default") -> AIAgent:
    """Create a fresh AIAgent per request. Uses the active provider from ProviderManager."""
    active = _pm.active
    if not active:
        raise RuntimeError("No active provider. Add one at /providers.")
    return AIAgent(
        session_id=session_id,
        provider_name=active,
        provider_manager=_pm,
        tool_registry=_tools,
        skills_index=_skills,
    )


# --- helpers ---

def _sessions_db() -> CrowState:
    """Short-lived DB handle for read-only queries (session list, search)."""
    return CrowState()


# --- metric config for dashboard ---
# Pluggable: add a dict entry to add a card. Each metric:
#   id, label, icon, color, value_fn(request) -> str
# value_fn runs on every dashboard load — add DB queries, shell calls, etc.

_DASHBOARD_METRICS: list[dict] = []
def _register_metric(label: str, icon: str, color: str, value_fn):
    _DASHBOARD_METRICS.append({
        "label": label, "icon": icon, "color": color, "value_fn": value_fn
    })

_register_metric("Products", "📦", "blue", lambda _: _pg_val("SELECT count(*) FROM products"))
_register_metric("Suppliers", "🏭", "green", lambda _: _pg_val("SELECT count(*) FROM suppliers"))
_register_metric("Prices", "💰", "amber", lambda _: _pg_val("SELECT count(*) FROM prices"))
_register_metric("Active Sources", "🔗", "purple", lambda _: _pg_val("SELECT count(*) FROM sources WHERE is_active = true"))
_register_metric("Cron Jobs", "⏰", "sky", lambda _: str(len(_cron.jobs())))
_register_metric("Sessions", "📂", "pink", lambda _: _sqlite_val("SELECT count(*) FROM sessions"))

def _pg_val(sql: str) -> str:
    if not PG_DSN:
        return "—"
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        cur.close(); conn.close()
        n = row[0]
        if isinstance(n, int) and n >= 1000:
            return f"{n:,}"
        return str(n) if n is not None else "0"
    except Exception:
        return "—"


def _sqlite_val(sql: str) -> str:
    try:
        db = _sessions_db()
        row = db._conn.execute(sql).fetchone()
        db.close()
        n = row[0]
        return f"{n:,}" if isinstance(n, int) and n >= 1000 else str(n or 0)
    except Exception:
        return "—"


# --- routes: pages ---


@app.get("/health")
async def health():
    """Health check with DB, provider, and cron status."""
    checks = {}
    healthy = True

    # DB check
    try:
        _task_db._conn.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = str(exc)
        healthy = False
        logger.warning("Health check FAIL: db — %s", exc)

    # Provider check
    try:
        active = _pm.active
        checks["provider"] = active if active else "none active"
        if not active:
            healthy = False
            logger.warning("Health check FAIL: provider — no active provider")
    except Exception as exc:
        checks["provider"] = str(exc)
        healthy = False
        logger.warning("Health check FAIL: provider — %s", exc)

    # Cron check
    try:
        cron_alive = _cron._task is not None and not _cron._task.done()
        checks["cron"] = "running" if cron_alive else "stopped"
        if not cron_alive:
            healthy = False
            logger.warning("Health check FAIL: cron — task=%s done=%s",
                           _cron._task is not None,
                           _cron._task.done() if _cron._task else "N/A")
    except Exception as exc:
        checks["cron"] = str(exc)
        healthy = False
        logger.warning("Health check FAIL: cron — %s", exc)

    # Heartbeat check
    try:
        hb_alive = _heartbeat is not None and _heartbeat._task is not None and not _heartbeat._task.done()
        checks["heartbeat"] = "running" if hb_alive else "stopped"
        if not hb_alive:
            healthy = False
            logger.warning("Health check FAIL: heartbeat — hb=%s task=%s done=%s",
                           _heartbeat is not None,
                           _heartbeat._task is not None if _heartbeat else "N/A",
                           _heartbeat._task.done() if _heartbeat and _heartbeat._task else "N/A")
    except Exception as exc:
        checks["heartbeat"] = str(exc)
        healthy = False

    code = 200 if healthy else 503
    return JSONResponse(content={"status": "ok" if healthy else "degraded", "checks": checks}, status_code=code)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    metrics = []
    for m in _DASHBOARD_METRICS:
        try:
            val = m["value_fn"](request)
        except Exception:
            val = "err"
        metrics.append({"label": m["label"], "icon": m["icon"], "color": m["color"], "value": val})
    # extra inline stats
    extra = {}
    try:
        db = _sessions_db()
        row = db._conn.execute("SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM turns").fetchone()
        db.close()
        extra["total_tokens"] = f"{row[0] + row[1]:,}"
        extra["prompt_tokens"] = f"{row[0]:,}"
        extra["completion_tokens"] = f"{row[1]:,}"
        # Estimate cost (deepseek-v4-flash: $0.09/1M prompt, $0.09/1M completion)
        cost = (row[0] * 0.09 + row[1] * 0.09) / 1_000_000
        extra["estimated_cost"] = f"${cost:.4f}"
    except Exception:
        extra = {"total_tokens": "—", "prompt_tokens": "—", "completion_tokens": "—", "estimated_cost": "—"}

    jobs = _cron.jobs()

    return templates.TemplateResponse(
        request, "dashboard.html",
        {"tab": "dashboard", "metrics": metrics, "extra": extra, "jobs": jobs}
    )


@app.get("/api/prices/search")
def price_search(request: Request, q: str = "") -> dict:
    if not q:
        return {"rows": []}
    try:
        if not PG_DSN:
            return {"rows": []}
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.name AS product, p.generic_name, s.name AS supplier,
                   pr.price, pr.currency, pr.unit, pr.scraped_at
            FROM prices pr
            JOIN sources src ON src.id = pr.source_id
            JOIN products p ON p.id = src.product_id
            JOIN suppliers s ON s.id = src.supplier_id
            WHERE p.name ILIKE %(pat)s
              AND pr.scraped_at = (
                  SELECT MAX(p2.scraped_at) FROM prices p2 WHERE p2.source_id = pr.source_id
              )
            ORDER BY pr.scraped_at DESC LIMIT 20
        """, {"pat": f"%{q}%"})
        rows = [dict(r, scraped_at=str(r["scraped_at"])[:10] if r["scraped_at"] else "") for r in cur.fetchall()]
        cur.close(); conn.close()
        return {"rows": rows}
    except Exception as e:
        return {"rows": [], "error": str(e)}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"tab": "chat"})


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request) -> HTMLResponse:
    db = _sessions_db()
    try:
        rows = db._conn.execute(
            "SELECT id, created_at, updated_at, "
            "(SELECT COUNT(*) FROM turns WHERE session_id = sessions.id) AS turn_count "
            "FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        sessions = [dict(r) for r in rows]
    finally:
        db.close()
    return templates.TemplateResponse(
        request, "sessions.html", {"tab": "sessions", "sessions": sessions}
    )


@app.get("/skills", response_class=HTMLResponse)
def skills_page(request: Request) -> HTMLResponse:
    skills = list(_skills.skills.values())
    return templates.TemplateResponse(
        request, "skills.html", {"tab": "skills", "skills": skills}
    )


@app.get("/cron", response_class=HTMLResponse)
def cron_page(request: Request) -> HTMLResponse:
    jobs = _cron.jobs()
    return templates.TemplateResponse(
        request, "cron.html", {"tab": "cron", "jobs": jobs}
    )


# --- routes: chat (SSE) ---


@app.get("/chat/stream")
async def chat_stream(request: Request, session_id: str = "default", message: str = "") -> StreamingResponse:
    """SSE endpoint. Streams agent response tokens as they arrive."""
    if not message:
        return StreamingResponse(iter([]), media_type="text/event-stream")

    async def event_generator() -> AsyncGenerator[str, None]:
        agent = _agent(session_id)
        try:
            async for event in agent.run_stream(message):
                if isinstance(event, str):
                    yield f"data: {json.dumps({'delta': event, 'done': False})}\n\n"
                elif isinstance(event, dict) and event.get("done"):
                    pass  # Hold done — drain tasks first

            # Drain any delegated tasks before closing the stream
            from crow_agent.task_registry import drain_and_execute, has_pending
            if has_pending():
                db = _sessions_db()
                async def _web_deliver(task_id: str, result: str, error: str | None) -> None:
                    text = f"❌ Task _{task_id}_ failed:\n\n{error}" if error else f"✅ Task _{task_id}_ done:\n\n{result}"
                    db.append_turn(session_id, "assistant", text)
                await drain_and_execute(deliver=_web_deliver, background=False)
                db.close()

            # Always yield done — includes task results in history
            history = agent.db.history(session_id, limit=50)
            html = _render_history_html(history)
            yield f"data: {json.dumps({'delta': '', 'done': True, 'history': html})}\n\n"

        except Exception as exc:
            logger.exception("Chat error")
            yield f"data: {json.dumps({'delta': f'Error: {exc}', 'done': True})}\n\n"
        finally:
            agent.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _render_history_html(history: list[dict]) -> str:
    """Render conversation history as HTML partial."""
    html_parts: list[str] = []
    for turn in history:
        role = turn["role"]
        content = _escape_html(turn.get("content", "") or "")
        cls = "user" if role == "user" else "assistant" if role == "assistant" else "system"
        html_parts.append(f'<div class="msg {cls}"><span class="role">{role}</span><div class="content">{content}</div></div>')
    return "".join(html_parts)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --- routes: session detail ---


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(request: Request, session_id: str) -> HTMLResponse:
    db = _sessions_db()
    try:
        history = db.history(session_id, limit=200)
    finally:
        db.close()
    return templates.TemplateResponse(
        request, "session_detail.html",
        {"tab": "sessions", "session_id": session_id, "history": history},
    )


@app.get("/sessions/{session_id}/history")
def session_history(request: Request, session_id: str) -> HTMLResponse:
    """Return conversation history as an HTML partial (for inline load in chat)."""
    db = _sessions_db()
    try:
        history = db.history(session_id, limit=200)
    finally:
        db.close()
    html = _render_history_html(history)
    return HTMLResponse(html)


@app.post("/sessions/{session_id}/fork")
def session_fork(request: Request, session_id: str) -> dict:
    new_id = f"{session_id}-fork"
    db = _sessions_db()
    try:
        ok = db.fork_session(session_id, new_id)
        return {"ok": ok, "new_id": new_id, "error": None if ok else "Session name already exists"}
    finally:
        db.close()


@app.post("/sessions/{session_id}/rename")
async def session_rename(request: Request, session_id: str) -> dict:
    form = await request.form()
    new_name = form.get("name", "").strip()
    if not new_name:
        return {"ok": False, "error": "Name is required"}
    db = _sessions_db()
    try:
        ok = db.rename_session(session_id, new_name)
        return {"ok": ok, "error": None if ok else "Session name already exists"}
    finally:
        db.close()


@app.get("/sessions/{session_id}/export")
def session_export(request: Request, session_id: str, format: str = "md") -> Response:
    db = _sessions_db()
    try:
        history = db.history(session_id, limit=200)
    finally:
        db.close()

    if not history:
        return HTMLResponse("Session not found or empty", status_code=404)

    if format == "json":
        import json
        data = {"session_id": session_id, "turns": history}
        return Response(
            content=json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )

    lines = [f"# Session: {session_id}\n"]
    for turn in history:
        role = turn["role"].upper()
        content = turn["content"]
        lines.append(f"## {role}\n\n{content}\n")
    text = "\n".join(lines)
    return Response(
        content=text,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@app.get("/sessions/{session_id}/tokens")
def session_tokens(request: Request, session_id: str) -> dict:
    db = _sessions_db()
    try:
        return db.token_totals(session_id)
    finally:
        db.close()


@app.post("/sessions/{session_id}/delete")
def session_delete(request: Request, session_id: str) -> dict:
    db = _sessions_db()
    try:
        db.delete_session(session_id)
        return {"ok": True}
    finally:
        db.close()


# --- routes: cron API ---


@app.post("/cron/run/{job_id}")
async def cron_run(request: Request, job_id: str) -> dict:
    job = _cron._jobs.get(job_id)
    if not job:
        return {"error": "Job not found"}
    result_holder: list[str] = []

    async def capture_runner(j):
        from crow_agent.run_agent import AIAgent, Trigger, TriggerSource
        a = _agent(f"cron-{j.id}")
        try:
            trigger = Trigger(source=TriggerSource.USER, prompt=j.prompt)
            r = a.run(trigger)
            result_holder.append(r[:500])
        finally:
            a.close()

    _cron.set_runner(capture_runner)
    await _cron._execute(job)
    return {"result": result_holder[0] if result_holder else "(no output)"}


@app.post("/cron/toggle/{job_id}")
def cron_toggle(request: Request, job_id: str) -> dict:
    job = _cron._jobs.get(job_id)
    if not job:
        return {"error": "Job not found"}
    job.enabled = not job.enabled
    _cron._save()
    return {"enabled": job.enabled}


@app.post("/cron/delete/{job_id}")
def cron_delete(request: Request, job_id: str) -> dict:
    _cron.remove_job(job_id)
    return {"ok": True}


@app.post("/cron/add")
async def cron_add(request: Request) -> dict:
    form = await request.form()
    job = _cron.add_job(
        job_id=form["job_id"],
        prompt=form["prompt"],
        interval_seconds=int(form["interval_seconds"]),
    )
    return {"id": job.id, "ok": True}


# --- routes: FTS search ---


@app.get("/search")
def search(request: Request, q: str = "", limit: int = 10) -> HTMLResponse:
    db = _sessions_db()
    try:
        results = db.search(q, limit=limit)
    finally:
        db.close()
    return templates.TemplateResponse(
        request, "search_results.html",
        {"query": q, "results": results},
    )


# --- routes: team ---


@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "team.html",
        {"tab": "team", "profiles": _profiles},
    )


# --- routes: tasks ---

@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, status: str = "") -> HTMLResponse:
    db = _sessions_db()
    tasks = db.list_tasks(status=status or None)
    return templates.TemplateResponse(
        request, "tasks.html",
        {"tab": "tasks", "tasks": tasks, "status": status},
    )


@app.post("/tasks/{task_id}/done")
def task_done(task_id: str) -> dict:
    db = _sessions_db()
    db.update_task(task_id, status="done", snoozed_until=None)
    return {"ok": True}


@app.post("/tasks/{task_id}/snooze")
def task_snooze(task_id: str) -> dict:
    from datetime import datetime, timedelta, timezone
    db = _sessions_db()
    snoozed = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.update_task(task_id, snoozed_until=snoozed)
    return {"ok": True}


# --- routes: providers ---


# Known model lists for curated dropdowns (keyed by provider name prefix)
PROVIDER_MODELS: dict[str, list[str]] = {
    "opencode": [
        "deepseek-v4-flash", "deepseek-v4-pro", "minimax-m3",
        "minimax-m2.7", "minimax-m2.5", "kimi-k2.7-code", "kimi-k2.6",
        "kimi-k2.5", "glm-5.1", "glm-5", "qwen3.7-max", "qwen3.7-plus",
        "qwen3.6-plus", "qwen3.5-plus", "mimo-v2-pro", "mimo-v2-omni",
        "mimo-v2.5-pro", "mimo-v2.5", "hy3-preview",
    ],
    "commandcode": [
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
        "MiniMaxAI/MiniMax-M3", "MiniMaxAI/MiniMax-M2.7",
        "MiniMaxAI/MiniMax-M2.5", "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K2.6", "moonshotai/Kimi-K2.5",
        "zai-org/GLM-5.1", "zai-org/GLM-5",
        "Qwen/Qwen3.7-Max", "Qwen/Qwen3.7-Plus",
        "Qwen/Qwen3.6-Max-Preview", "Qwen/Qwen3.6-Plus",
        "xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5",
        "stepfun/Step-3.7-Flash", "stepfun/Step-3.5-Flash",
        "google/gemini-3.5-flash", "google/gemini-3.1-flash-lite",
        "nvidia/nemotron-3-ultra-550b-a55b",
    ],
}

def _models_for(name: str) -> list[str]:
    """Return curated models for a provider name, or empty if unknown."""
    for prefix, models in PROVIDER_MODELS.items():
        if name.lower().startswith(prefix):
            return models
    return []


@app.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request) -> HTMLResponse:
    entries = _pm.all_entries()
    # Precompute available models for each provider
    enriched = []
    for p in entries:
        models = _models_for(p.name)
        enriched.append({"entry": p, "available_models": models})
    return templates.TemplateResponse(
        request, "providers.html",
        {"tab": "providers", "providers": enriched, "active": _pm.active,
         "provider_models": PROVIDER_MODELS},
    )


@app.post("/providers/add")
async def providers_add(request: Request) -> dict:
    form = await request.form()
    existing = _pm.get(form["name"])
    entry = ProviderEntry(
        name=form["name"],
        base_url=form["base_url"],
        model=form["model"],
        api_key=form["api_key"],
        api_type=form.get("api_type", existing.api_type if existing else "openai_compat"),
        reasoning_variance=existing.reasoning_variance if existing else "",
    )
    _pm.add(entry)
    return {"ok": True, "name": entry.name}


@app.post("/providers/delete/{name}")
def providers_delete(name: str) -> dict:
    ok = _pm.delete(name)
    return {"ok": ok}


@app.post("/providers/active/{name}")
def providers_set_active(name: str) -> dict:
    ok = _pm.set_active(name)
    return {"ok": ok, "active": _pm.active}


# ── Activity Kanban ────────────────────────────────────────────────




@app.get('/api/recover')
def recover_session(session_id: str = ''):
    try:
        db = _sessions_db()
        turns = db.history(session_id or 'default', limit=10)
        db.close()
        return JSONResponse({'session': session_id, 'turns': len(turns)})
    except Exception as e:
        return JSONResponse({'error': str(e)})

@app.get('/activity/replay')
def activity_replay(request, limit: int = 50):
    trail = []
    try:
        db = _sessions_db()
        for r in db._conn.execute('SELECT tool_name, substr(arguments,1,200) as args, substr(output,1,200) as output, created_at FROM tool_outputs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall():
            trail.append({'time': r['created_at'], 'tool': r['tool_name'], 'args': r['args'], 'output': r['output']})
        db.close()
    except Exception:
        logger.debug("activity_replay query failed", exc_info=True)
    return JSONResponse({'trail': trail})





try:
    from extensions.web_ui.server import app as web_ui
    app.mount("/ui", web_ui)
except Exception:
    pass

