"""Web tools: search, fetch, browser_fetch — backed by httpx + trafilatura + Playwright."""

from __future__ import annotations

import os
import threading
from typing import Any

from .tools_common import is_private_host


# ponytail: module-level Playwright singleton — one browser, one context per session
_browser_lock = threading.Lock()
_browser: Any = None
_browser_context: Any = None
_pw_handle: Any = None
_current_page: Any = None


def _check_playwright() -> bool:
    """Check if Playwright + Chromium are available."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _browser_ensure() -> None:
    """Lazy-init headless Chromium browser + context."""
    global _browser, _browser_context, _pw_handle
    if _browser is not None:
        return
    with _browser_lock:
        if _browser is not None:
            return
        from playwright.sync_api import sync_playwright
        _pw_handle = sync_playwright().start()
        _browser = _pw_handle.chromium.launch(headless=True)
        _browser_context = _browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )


def _browser_page() -> Any:
    """Return current page, creating a new one if needed."""
    global _current_page
    _browser_ensure()
    if _current_page is None or _current_page.is_closed():
        _current_page = _browser_context.new_page()
    return _current_page


def _extract_text(html: str) -> str:
    """Extract clean article text from HTML using trafilatura."""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_links=True, output_format="markdown")
        return text or "No readable content extracted."
    except ImportError:
        return "trafilatura not installed. Run: pip install trafilatura"


def _httpx_get(url: str, timeout: int = 30) -> str:
    """Fetch URL via httpx with SSRF guard."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked: only http/https URLs allowed, got '{parsed.scheme}'"
    host = parsed.hostname or ""
    if is_private_host(host):
        return f"Blocked: '{host}' is a private/internal address"

    import httpx
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; CrowBot/1.0)"})
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        return f"Fetch error: {exc}"


def _duckduckgo_search(query: str, max_results: int = 5) -> str:
    """Search via DuckDuckGo (ddgs library). No API key needed."""
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=max_results))
    except Exception as exc:
        return f"Search error: {exc}"

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}")
        lines.append(f"   {r.get('href', '')}")
        body = (r.get("body", "") or "")[:500]
        if body:
            lines.append(f"   {body}")
        lines.append("")
    return "\n".join(lines) if lines else f"No results for '{query}'"


def _playwright_fetch(url: str) -> str:
    """Fetch page content via Playwright (handles JS rendering)."""
    import subprocess
    script = f"""
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("{url}", timeout=30000, wait_until="domcontentloaded")
        content = await page.content()
        await browser.close()
        if len(content) > 50000:
            content = content[:50000]
        sys.stdout.write(content)

asyncio.run(run())
"""
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        html = result.stdout.strip()
        text = _extract_text(html) if not html.startswith("Error") else html
        return text
    except Exception as exc:
        return f"Browser fetch error: {exc}"




def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register web tools."""

    @registry.register(description="Search the web. Returns structured results with titles, URLs, and descriptions. Free — no API key needed.")
    def web_search(query: str, max_results: int = 5) -> str:
        """Search Google via Playwright. No API key needed."""
        return _duckduckgo_search(query, max_results)

    @registry.register(description="Fetch and extract clean content from a URL. Returns summarized markdown via sandboxed leaf agent.")
    def web_fetch(url: str) -> str:
        """Fetch a URL, extract clean article text. Content is summarized by a sandboxed leaf agent."""
        html = _httpx_get(url)
        if html.startswith(("Blocked", "Fetch error")):
            return html

        text = _extract_text(html)
        if not text or len(text) < 50:
            # Fallback: use Playwright if httpx got nothing meaningful
            text = _playwright_fetch(url)
        if not text or text.startswith(("Blocked", "Fetch error", "Browser fetch error")):
            return text

        if len(text) > 50000:
            text = text[:50000] + "\n\n... truncated at 50000 chars"

        from .agent_profiles import load_all_profiles
        from .providers import resolve_provider, ChatMessage
        from .provider_manager import ProviderManager

        profiles = load_all_profiles()
        profile = profiles.get("web-reader")
        if not profile:
            return f"Leaf agent 'web-reader' not found. Create team/web-reader.md"

        messages = [
            ChatMessage(role="system", content=profile.instructions),
            ChatMessage(role="user", content=text),
        ]
        pm = ProviderManager()
        provider = resolve_provider(pm.active, provider_manager=pm)
        return provider.chat(messages, tools=None).content

    @registry.register(
        description="Fetch a web page using a headless browser (JS rendered). Returns raw text. Use for dynamic sites.",
        name="browser_fetch",
    )
    def browser_fetch(url: str = "", timeout: int = 15000) -> str:
        """Fetch a JS-rendered page via Playwright. Returns clean text."""
        if not url:
            return "Error: url is required"

        text = _playwright_fetch(url)
        if text.startswith(("Blocked", "Browser fetch error")):
            return text
        return text[:10000].strip()

    @registry.register(
        name="scrape_page",
        description="Scrape a web page using Scrapling (handles Cloudflare, JS, anti-bot). Falls back to httpx+trafilatura if Scrapling unavailable. Use render_js=True for JS-heavy sites.",
    )
    def scrape_page(url: str = "", render_js: bool = False) -> str:
        """Scrape a page with Scrapling's adaptive engine."""
        if not url:
            return "Error: url is required"

        try:
            from scrapling import Fetcher
            fetcher = Fetcher(auto_match=True)
            resp = fetcher.get(url)
            if resp.status_code != 200:
                return f"Scrapling returned HTTP {resp.status_code}"
            text = _extract_text(resp.text)
            return text[:10000].strip()
        except ImportError:
            # Fallback to httpx + trafilatura
            html = _httpx_get(url)
            return _extract_text(html)[:10000].strip()
        except Exception as exc:
            # Try fallback on any Scrapling error
            try:
                html = _httpx_get(url)
                return _extract_text(html)[:10000].strip()
            except Exception:
                return f"Scrape error: {exc}"

    # ── Browser interaction tools (Playwright sync API) ──────────────

    @registry.register(
        description="Navigate browser to a URL. Must be called first before other browser_* tools. Returns page title.",
        check_fn=lambda: _check_playwright(),
    )
    def browser_navigate(url: str) -> str:
        if not url:
            return "Error: url is required"
        page = _browser_page()
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            return f"Navigated to {page.title()}"
        except Exception as exc:
            return f"Navigation error: {exc}"

    @registry.register(
        description="Get a text snapshot of the current page's interactive elements with ref IDs (like '@e1', '@e2'). Use ref IDs to click/type.",
        check_fn=lambda: _check_playwright(),
    )
    def browser_snapshot(full: bool = False) -> str:
        page = _browser_page()
        elements = page.query_selector_all("a, button, input, textarea, select, [tabindex], [role=button], [role=link]")
        lines = []
        for i, el in enumerate(elements):
            ref = f"@e{i}"
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            text = el.inner_text().strip()[:80]
            type_attr = el.get_attribute("type") or ""
            href = el.get_attribute("href") or ""
            parts = [f"[{ref}] <{tag}>", text]
            if type_attr:
                parts.append(f"type={type_attr}")
            if href and not href.startswith("javascript"):
                parts.append(f"href={href}")
            lines.append(" ".join(parts))
        if not lines:
            return "No interactive elements found on this page."
        return "\n".join(lines[: (200 if full else 80)])

    @registry.register(
        description="Click on an element by its ref ID from browser_snapshot (e.g. '@e5').",
        check_fn=lambda: _check_playwright(),
    )
    def browser_click(ref: str) -> str:
        page = _browser_page()
        elements = page.query_selector_all("a, button, input, textarea, select, [tabindex], [role=button], [role=link]")
        try:
            idx = int(ref.lstrip("@e"))
            if idx < 0 or idx >= len(elements):
                return f"Error: ref {ref} out of range (0-{len(elements)-1})"
            elements[idx].click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            return f"Clicked {ref}. Page: {page.title()}"
        except (ValueError, IndexError):
            return f"Error: invalid ref '{ref}'. Use format '@e0', '@e1' from browser_snapshot."
        except Exception as exc:
            return f"Click error: {exc}"

    @registry.register(
        description="Type text into an input field identified by its ref ID from browser_snapshot.",
        check_fn=lambda: _check_playwright(),
    )
    def browser_type(ref: str, text: str) -> str:
        page = _browser_page()
        elements = page.query_selector_all("a, button, input, textarea, select, [tabindex], [role=button], [role=link]")
        try:
            idx = int(ref.lstrip("@e"))
            if idx < 0 or idx >= len(elements):
                return f"Error: ref {ref} out of range (0-{len(elements)-1})"
            el = elements[idx]
            el.fill("")
            el.fill(text)
            return f"Typed into {ref}: '{text[:80]}{'...' if len(text) > 80 else ''}'"
        except (ValueError, IndexError):
            return f"Error: invalid ref '{ref}'"
        except Exception as exc:
            return f"Type error: {exc}"

    @registry.register(
        description="Press a keyboard key on the current page (e.g. 'Enter', 'Tab', 'Escape').",
        check_fn=lambda: _check_playwright(),
    )
    def browser_press(key: str) -> str:
        page = _browser_page()
        try:
            page.keyboard.press(key)
            return f"Pressed '{key}'"
        except Exception as exc:
            return f"Press error: {exc}"

    @registry.register(
        description="Scroll the page in a direction: 'down', 'up', 'bottom', 'top'.",
        check_fn=lambda: _check_playwright(),
    )
    def browser_scroll(direction: str) -> str:
        page = _browser_page()
        d = direction.strip().lower()
        try:
            if d == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif d == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif d == "down":
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            elif d == "up":
                page.evaluate("window.scrollBy(0, -window.innerHeight * 0.8)")
            else:
                return f"Error: unknown direction '{direction}'. Use: up, down, top, bottom"
            return f"Scrolled {d}"
        except Exception as exc:
            return f"Scroll error: {exc}"

    @registry.register(
        description="Go back to the previous page in browser history.",
        check_fn=lambda: _check_playwright(),
    )
    def browser_back() -> str:
        page = _browser_page()
        try:
            page.go_back()
            return f"Went back to {page.title()}"
        except Exception as exc:
            return f"Back error: {exc}"

    @registry.register(
        description="Get the text content of the current page (useful after navigation/click).",
        check_fn=lambda: _check_playwright(),
    )
    def browser_page_text() -> str:
        page = _browser_page()
        try:
            text = page.inner_text("body")
            return text[:5000].strip() or "(empty page)"
        except Exception as exc:
            return f"Page text error: {exc}"

