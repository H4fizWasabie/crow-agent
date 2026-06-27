"""Auto Builder — procedural build engine for Crow Auto Builder skill.

Orchestrates the build pipeline: scaffold → code gen → test → verify → package.
Called by TelegramBot._cmd_build or triggered via skill matching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .providers import ChatMessage, ChatResponse, resolve_provider, ProviderConfig

logger = logging.getLogger("crow_agent.auto_builder")

BUILDS_DIR = Path.home() / "crow-builds"
ENV_FILE = BUILDS_DIR / ".env"

# ── helpers ──

def _load_env() -> dict[str, str]:
    """Read ~/crow-builds/.env into a dict."""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        logger.warning("Builder .env not found at %s", ENV_FILE)
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def _app_dir(name: str) -> Path:
    return BUILDS_DIR / name


def _spec_path(name: str) -> Path:
    return _app_dir(name) / "spec.md"


def _state_path(name: str) -> Path:
    return _app_dir(name) / "state.json"


def _read_state(name: str) -> dict[str, Any]:
    p = _state_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"app_name": name, "phases": {}, "error": None}


def _write_state(name: str, state: dict[str, Any]) -> None:
    _state_path(name).write_text(json.dumps(state, indent=2))


async def _tg_send_async(token: str, chat_id: int, text: str) -> None:
    """Send a Telegram message asynchronously."""
    import httpx
    async with httpx.AsyncClient() as client:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        await client.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)


def _retry_async(coro_factory, max_retries=3, delay=5):
    """Run an async coroutine with retries."""
    async def wrapper():
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                return await coro_factory()
            except Exception as exc:
                last_exc = exc
                logger.warning("Attempt %d/%d failed: %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore
    return wrapper()


# ── Builder model client ──

class BuilderClient:
    """Thin wrapper around providers.py to call the Builder model."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        self._provider = self._init_provider()

    def _init_provider(self):
        api_key = self._env.get("OPENROUTER_API_KEY", "")
        model = self._env.get("BUILDER_MODEL", "openrouter/owl-alpha")
        cfg = ProviderConfig(
            name="openrouter",
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            model=model,
            api_type="openai_compat",
        )
        return resolve_provider(cfg)

    def chat(self, messages: list[ChatMessage]) -> ChatResponse:
        return self._provider.chat(messages, max_tokens=32000)


# ── Phase implementations ──

def _phase_scaffold(spec: str, builder: BuilderClient, app_dir: Path) -> list[str]:
    """Phase 1: Ask Builder for project structure, create directories/files."""
    prompt = (
        f"Spec: {spec}\n\n"
        "Design the project structure for this app. "
        "Choose the best stack (Python/JS/Go etc) based on the requirements.\n"
        "Output:\n"
        "1. Stack choice + reasoning (1 line)\n"
        "2. Directory/file tree\n"
        "3. For each file: a brief description of its purpose\n"
        "4. Key dependencies/packages\n\n"
        "I will create these files. Be concise and precise."
    )
    resp = builder.chat([ChatMessage(role="user", content=prompt)])
    scaffold_plan = resp.content

    # Parse directory/file tree and create structure
    created: list[str] = []
    for line in scaffold_plan.splitlines():
        stripped = line.strip()
        # Match lines that look like file paths (e.g. "src/app.py", "  src/  app.py")
        # Heuristic: lines ending with .py, .js, .html, .css, .json, .yaml, .md, or /
        if any(stripped.endswith(ext) for ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".yaml", ".md", "/", ":")):
            # Extract path: remove tree chars (├──, └──, │, ─, etc.)
            clean = stripped.lstrip(" │├└─").strip().rstrip(":")
            if clean and not clean.startswith(("#", "//", "-", ".")):
                fp = app_dir / clean
                if clean.endswith("/"):
                    fp.mkdir(parents=True, exist_ok=True)
                else:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    if not fp.exists():
                        fp.write_text(f"# {clean}\n")
                    created.append(clean)

    if not created:
        # Fallback: create placeholder
        (app_dir / "src").mkdir(parents=True, exist_ok=True)
        (app_dir / "src" / "app.py").write_text("# placeholder\n")
        created.append("src/app.py")

    return created


def _phase_build(spec: str, builder: BuilderClient, app_dir: Path, files: list[str]) -> int:
    """Phase 2: Build each file via Builder model."""
    total_lines = 0
    for fpath in files:
        full_path = app_dir / fpath
        if not full_path.exists() or full_path.stat().st_size == 0:
            prompt = (
                f"Project: {app_dir.name}\n"
                f"Spec: {spec[:2000]}\n\n"
                f"File: {fpath}\n"
                f"Write the complete {fpath} file. Include imports, error handling.\n"
                f"Output ONLY the file content, no explanations."
            )
            resp = builder.chat([ChatMessage(role="user", content=prompt)])
            content = resp.content.strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                first = content.find("\n")
                if first != -1:
                    content = content[first + 1:]
                if content.endswith("```"):
                    content = content[:-3].strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            total_lines += content.count("\n") + 1
    return total_lines


def _phase_test(app_dir: Path) -> list[str]:
    """Phase 3: Start dev server, screenshot, kill."""
    screenshots: list[str] = []
    screenshot_dir = app_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Try to start dev server
    server_process = None
    port = None
    try:
        if (app_dir / "requirements.txt").exists():
            subprocess.run(["pip", "install", "-r", "requirements.txt"], cwd=str(app_dir), capture_output=True, timeout=120)
        if (app_dir / "app.py").exists():
            server_process = subprocess.Popen(["python", "app.py"], cwd=str(app_dir))
            port = 5000
        elif (app_dir / "main.py").exists():
            server_process = subprocess.Popen(["python", "main.py"], cwd=str(app_dir))
            port = 8000
        elif (app_dir / "package.json").exists():
            subprocess.run(["npm", "install"], cwd=str(app_dir), capture_output=True, timeout=120)
            server_process = subprocess.Popen(["npm", "run", "dev"], cwd=str(app_dir), shell=True)
            port = 3000

        if port and server_process:
            # Wait for server
            import time
            time.sleep(5)
            # Try to screenshot with browser_tool if available
            try:
                # Use curl as fallback for basic health check
                result = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"http://localhost:{port}"], capture_output=True, timeout=10)
                if result.stdout.strip() == "000":
                    logger.warning("Dev server not responding on port %d", port)
                else:
                    screenshots.append(f"http://localhost:{port} → status {result.stdout.strip()}")
            except Exception as exc:
                logger.warning("Screenshot failed: %s", exc)
    except Exception as exc:
        logger.warning("Dev server error: %s", exc)
    finally:
        if server_process:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()

    return screenshots


def _phase_verify(spec: str, app_dir: Path) -> list[dict[str, str]]:
    """Phase 4: Verify files exist, check basic structure."""
    results: list[dict[str, str]] = []
    files = list(app_dir.rglob("*"))
    py_files = [f for f in files if f.suffix == ".py"]
    js_files = [f for f in files if f.suffix in (".js", ".ts", ".jsx", ".tsx")]
    html_files = [f for f in files if f.suffix == ".html"]

    results.append({"check": "Source files exist", "status": "PASS" if len(py_files + js_files + html_files) > 0 else "FAIL"})
    results.append({"check": "Python files valid syntax", "status": "PASS" if all(_check_py_syntax(f) for f in py_files) else "FAIL"})
    return results


def _check_py_syntax(path: Path) -> bool:
    try:
        compile(path.read_text(), str(path), "exec")
        return True
    except SyntaxError:
        return False


def _phase_package(app_dir: Path) -> Path:
    """Phase 5: Zip app (exclude deps)."""
    import shutil
    zip_path = app_dir.parent / f"{app_dir.name}.zip"
    shutil.make_archive(
        str(zip_path.with_suffix("")),  # strip .zip for make_archive
        "zip",
        root_dir=str(app_dir),
        base_dir=".",
    )
    return zip_path if zip_path.exists() else _rezip(app_dir)


def _rezip(app_dir: Path) -> Path:
    """Fallback: use system zip command."""
    zip_path = app_dir.parent / f"{app_dir.name}.zip"
    excludes = ["*/node_modules/*", "*/venv/*", "*/__pycache__/*", ".venv/*", "*/site-packages/*"]
    cmd = ["zip", "-r", str(zip_path), "."] + [f"-x{e}" for e in excludes]
    subprocess.run(cmd, cwd=str(app_dir), capture_output=True, timeout=60)
    return zip_path


# ── Public API ──

def check_spec(name: str) -> str | None:
    """Validate that a frozen spec exists. Returns None if ok, error string if not."""
    if "/" in name or ".." in name:
        return "Invalid app name"
    spec = _spec_path(name)
    if not spec.exists():
        return f"No frozen spec at {spec}. Grill and freeze spec first."
    return None


def check_env() -> str | None:
    """Validate builder .env. Returns None if ok, error string if not."""
    if not ENV_FILE.exists():
        return f"Builder .env not found at {ENV_FILE}. Create with OPENROUTER_API_KEY and BUILDER_MODEL."
    env = _load_env()
    if "OPENROUTER_API_KEY" not in env:
        return "OPENROUTER_API_KEY missing from .env"
    return None


async def run_build(name: str, tg_token: str, tg_chat_id: int, tg_bot=None) -> dict[str, Any]:
    """Run the full build pipeline for an app. Sends Telegram updates.

    Returns the final state dict.
    """
    app_dir = _app_dir(name)
    app_dir.mkdir(parents=True, exist_ok=True)
    spec = _spec_path(name).read_text()
    env = _load_env()

    state = _read_state(name)
    state["phases"]["scaffold"] = "pending"
    state["phases"]["build"] = "pending"
    state["phases"]["test"] = "pending"
    state["phases"]["verify"] = "pending"
    state["phases"]["package"] = "pending"
    state["error"] = None

    async def _update(msg: str):
        if tg_bot:
            try:
                await tg_bot.send_message(chat_id=tg_chat_id, text=msg)
            except Exception:
                logger.debug("Telegram build notification failed", exc_info=True)
        else:
            await _tg_send_async(tg_token, tg_chat_id, msg)

    await _update(f"🏗️ Auto Builder started: **{name}**")

    # Phase 1: Scaffold
    try:
        await _update(f"🏗️ Phase 1: Scaffold — designing project structure...")
        builder = BuilderClient(env)
        files = await asyncio.to_thread(_phase_scaffold, spec, builder, app_dir)
        state["phases"]["scaffold"] = "done"
        _write_state(name, state)
        await _update(f"✅ Scaffold done — {len(files)} files planned")
    except Exception as exc:
        logger.exception("Phase 1 failed")
        state["phases"]["scaffold"] = "failed"
        state["error"] = f"Scaffold failed: {exc}"
        _write_state(name, state)
        await _update(f"⚠️ Phase 1 failed: {exc}")
        return state

    # Phase 2: Build
    try:
        await _update(f"🏗️ Phase 2: Build — writing source files...")
        total_lines = await asyncio.to_thread(_phase_build, spec, builder, app_dir, files)
        state["phases"]["build"] = "done"
        _write_state(name, state)
        await _update(f"✅ Build done — {len(files)} files, {total_lines} lines")
    except Exception as exc:
        logger.exception("Phase 2 failed")
        state["phases"]["build"] = "failed"
        state["error"] = f"Build failed: {exc}"
        _write_state(name, state)
        await _update(f"⚠️ Phase 2 failed: {exc}")
        return state

    # Phase 3: Test
    try:
        await _update(f"🏗️ Phase 3: Test — starting dev server, capturing screenshots...")
        screenshots = await asyncio.to_thread(_phase_test, app_dir)
        state["phases"]["test"] = "done" if screenshots else "partial"
        _write_state(name, state)
        await _update(f"📸 Test done — {len(screenshots)} checks")
    except Exception as exc:
        logger.exception("Phase 3 failed")
        state["phases"]["test"] = "failed"
        state["error"] = f"Test failed: {exc}"
        _write_state(name, state)
        await _update(f"⚠️ Phase 3 failed: {exc}")
        return state

    # Phase 4: Verify
    try:
        await _update(f"🏗️ Phase 4: Verify — checking source files...")
        results = await asyncio.to_thread(_phase_verify, spec, app_dir)
        failures = [r for r in results if r["status"] == "FAIL"]
        state["phases"]["verify"] = "done" if not failures else "failed"
        _write_state(name, state)
        await _update(f"{'✅' if not failures else '⚠️'} Verify done — {len(results)} checks, {len(failures)} failures")
    except Exception as exc:
        logger.exception("Phase 4 failed")
        state["phases"]["verify"] = "failed"
        state["error"] = f"Verify failed: {exc}"
        _write_state(name, state)

    # Phase 5: Package
    try:
        await _update(f"🏗️ Phase 5: Package — zipping artifact...")
        zip_path = await asyncio.to_thread(_phase_package, app_dir)
        state["phases"]["package"] = "done"
        _write_state(name, state)
        await _update(f"📦 Package done — {zip_path.name} ({zip_path.stat().st_size / 1024:.0f} KB)")

        # Send zip to Telegram
        if zip_path.exists():
            try:
                if tg_bot:
                    with open(zip_path, "rb") as f:
                        await tg_bot.send_document(chat_id=tg_chat_id, document=f, filename=zip_path.name)
                else:
                    import httpx
                    async with httpx.AsyncClient() as client:
                        url = f"https://api.telegram.org/bot{tg_token}/sendDocument"
                        files = {"document": (zip_path.name, zip_path.read_bytes(), "application/zip")}
                        await client.post(url, data={"chat_id": tg_chat_id}, files=files)
            except Exception as exc:
                logger.warning("Failed to send zip via Telegram: %s", exc)
                await _update(f"Zip saved at: {zip_path}")
    except Exception as exc:
        logger.exception("Phase 5 failed")
        state["phases"]["package"] = "failed"
        state["error"] = f"Package failed: {exc}"
        _write_state(name, state)
        await _update(f"⚠️ Phase 5 failed: {exc}")

    # Final summary
    status = "✅" if state["error"] is None else "⚠️"
    await _update(
        f"{status} Auto Builder — Complete\n\n"
        f"App: {name}\n"
        f"Phases: {dict(state['phases'])}\n"
        f"How to run: see how-to-run.md in the zip"
    )

    return state
