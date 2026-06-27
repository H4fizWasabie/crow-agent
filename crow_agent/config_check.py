"""Startup configuration validation.

Checks env vars, provider config, and critical paths before the agent runs.
Provides PROJECT_ROOT-based path resolution for all modules.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .paths import PROJECT_ROOT

logger = logging.getLogger("crow_agent.config")


def resolve_path(relative: str) -> Path:
    """Resolve a project-relative path against PROJECT_ROOT.

    Always uses PROJECT_ROOT so CWD doesn't matter.
    """
    return PROJECT_ROOT / relative


def validate_config() -> list[str]:
    """Returns list of blocking errors. Empty list = config OK.

    Also prints warnings to stderr for non-critical issues.
    """
    errors: list[str] = []
    providers_path = Path.home() / ".crow_agent" / "providers.json"

    # ── Blocking checks ──

    if not providers_path.exists():
        # Check if .env-based provider is configured instead
        has_env_provider = any(
            os.environ.get(f"{pfx}_API_KEY")
            for pfx in ("OPENCODE_GO", "OPENROUTER", "OPENAI", "ANTHROPIC", "GROQ", "TOGETHER")
        )
        if not has_env_provider:
            errors.append(
                "No API keys found. Configure at least one provider:\n"
                "  • Set OPENROUTER_API_KEY in .env (free at https://openrouter.ai/keys)\n"
                "  • Or set any {NAME}_API_KEY / {NAME}_BASE_URL / {NAME}_MODEL env vars"
            )
        return errors  # can't check providers.json further

    try:
        data = json.loads(providers_path.read_text())
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid providers.json: {exc}")
        return errors

    active = data.get("active", "")
    providers = data.get("providers", {})

    if not active:
        errors.append("No active provider set in providers.json")
    elif active not in providers:
        errors.append(f"Active provider '{active}' not found in providers list")

    for name, cfg in providers.items():
        if not cfg.get("api_key", "").strip():
            errors.append(f"Provider '{name}' has empty API key")
        if not cfg.get("base_url", "").strip():
            errors.append(f"Provider '{name}' has empty base URL")
        if not cfg.get("model", "").strip():
            errors.append(f"Provider '{name}' has empty model")

    # ── Non-blocking warnings ──
    from crow_agent.crow_state import _db_path
    db_path = _db_path()
    if not db_path.exists():
        logger.warning("No sessions.db found — will be created on first use")

    telegram_key = os.environ.get("TELEGRAM_TOKEN", "")
    if not telegram_key:
        logger.warning("TELEGRAM_TOKEN not set — Telegram bot unavailable")

    skills_dir = resolve_path("skills")
    if not skills_dir.exists():
        logger.warning("skills/ directory not found — skills system disabled")

    # ── Provider connectivity test (blocking on auth, warning on transient) ──
    if not errors:
        try:
            cfg = providers[active]
            import httpx
            test_payload = {
                "model": cfg["model"],
                "messages": [{"role": "system", "content": "Respond with just 'ok'."}],
                "max_tokens": 10,
            }
            test_resp = httpx.post(
                f"{cfg['base_url'].rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json=test_payload,
                timeout=15,
            )
            if test_resp.status_code in (401, 403):
                errors.append(f"Provider '{active}' rejected auth ({test_resp.status_code}) — check API key")
            elif test_resp.status_code >= 500:
                logger.warning("Provider '%s' returned %s (may be transient)", active, test_resp.status_code)
            else:
                test_resp.raise_for_status()
        except httpx.TimeoutException:
            logger.warning("Provider '%s' timed out during connectivity test (may be transient)", active)
        except httpx.HTTPStatusError as exc:
            logger.warning("Provider '%s' connectivity test failed: %s (may be transient)", active, exc)
        except Exception as exc:
            logger.warning("Provider '%s' connectivity test error: %s", active, exc)

    return errors


def check_or_exit() -> None:
    """Run validation, print errors, exit if critical."""
    errors = validate_config()
    if errors:
        logger.error("Config errors:")
        for e in errors:
            logger.error("  ✗ %s", e)
        sys.exit(1)
