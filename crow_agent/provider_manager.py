"""Provider manager — JSON-backed provider config store.

Providers are stored in ~/.crow_agent/providers.json.
Each provider has: name, base_url, model, api_key, api_type.
One provider is marked as "active" (used by default).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_STORE_PATH = Path.home() / ".crow_agent" / "providers.json"

logger = logging.getLogger("crow_agent.provider_manager")


@dataclass
class ProviderEntry:
    """A single provider configuration."""
    name: str
    base_url: str
    model: str
    api_key: str
    api_type: str = "openai_compat"
    reasoning_variance: str = ""


class ProviderManager:
    """CRUD + persistence for provider configs."""

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._path = Path(store_path) if store_path else DEFAULT_STORE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"active": None, "providers": {}}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, KeyError):
                    self._data = {"active": None, "providers": {}}

    def _save(self) -> None:
        with self._lock:
            try:
                self._path.write_text(
                    json.dumps(self._data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.error("Failed to save providers: %s", exc)
                raise

    # --- CRUD ---

    def all_entries(self) -> list[ProviderEntry]:
        """Return all providers as ProviderEntry objects."""
        entries = []
        for name, p in self._data.get("providers", {}).items():
            entries.append(ProviderEntry(
                name=name,
                base_url=p.get("base_url", ""),
                model=p.get("model", ""),
                api_key=p.get("api_key", ""),
                api_type=p.get("api_type", "openai_compat"),
            ))
        return entries

    def get(self, name: str) -> ProviderEntry | None:
        p = self._data.get("providers", {}).get(name)
        if not p:
            return None
        return ProviderEntry(
            name=name,
            base_url=p.get("base_url", ""),
            model=p.get("model", ""),
            api_key=p.get("api_key", ""),
            api_type=p.get("api_type", "openai_compat"),
            reasoning_variance=p.get("reasoning_variance", ""),
        )

    def add(self, entry: ProviderEntry) -> None:
        """Add or replace a provider."""
        with self._lock:
            self._data["providers"][entry.name] = {
                "base_url": entry.base_url,
                "model": entry.model,
                "api_key": entry.api_key,
                "api_type": entry.api_type,
                "reasoning_variance": entry.reasoning_variance,
            }
            # Auto-set as active if it's the first one
            if self._data.get("active") is None:
                self._data["active"] = entry.name
            try:
                self._save()
            except OSError:
                logger.warning("Provider added but config not saved — changes lost on restart")

    def delete(self, name: str) -> bool:
        """Remove a provider. Returns True if it existed."""
        with self._lock:
            if name in self._data.get("providers", {}):
                del self._data["providers"][name]
                if self._data.get("active") == name:
                    # Pick another active provider, or None
                    remaining = list(self._data["providers"].keys())
                    self._data["active"] = remaining[0] if remaining else None
                try:
                    self._save()
                except OSError:
                    logger.warning("Provider deleted but config not saved — changes lost on restart")
                return True
            return False

    def set_active(self, name: str) -> bool:
        """Set the active provider."""
        with self._lock:
            if name in self._data.get("providers", {}):
                self._data["active"] = name
                try:
                    self._save()
                except OSError:
                    logger.warning("Active provider set but config not saved — changes lost on restart")
                return True
            return False

    @property
    def active(self) -> str | None:
        return self._data.get("active")

    @property
    def active_entry(self) -> ProviderEntry | None:
        """Return the active provider entry."""
        name = self.active
        if name:
            return self.get(name)
        return None

    def seed_from_env(self) -> int:
        """Seed providers from env vars. Adds new ones even if config already exists.

        Scans for {NAME}_API_KEY, {NAME}_BASE_URL, {NAME}_MODEL env vars.
        Only adds providers that don't already exist in the config.
        Returns number of providers seeded.
        """
        count = 0
        # Collect all unique provider prefixes from env
        prefixes: set[str] = set()
        for key in os.environ:
            if key.endswith("_API_KEY") and key != "API_KEY":
                prefix = key[: -len("_API_KEY")]
                # Verify the companion vars exist
                base_url_key = f"{prefix}_BASE_URL"
                model_key = f"{prefix}_MODEL"
                if os.environ.get(base_url_key) and os.environ.get(model_key):
                    prefixes.add(prefix)

        existing = set(self._data.get("providers", {}).keys())
        for prefix in sorted(prefixes):
            name = prefix.lower().replace("_", "-")
            if name in existing:
                continue  # Preserve existing config (may have been edited via UI)
            entry = ProviderEntry(
                name=name,
                base_url=os.environ[f"{prefix}_BASE_URL"],
                model=os.environ[f"{prefix}_MODEL"],
                api_key=os.environ[f"{prefix}_API_KEY"],
            )
            self.add(entry)
            count += 1

        return count
