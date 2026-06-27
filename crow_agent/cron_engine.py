"""Async cron scheduler with JSON job index.

Jobs are defined in a JSON file. The engine sleeps between ticks, checks which
jobs are due, and spawns them as isolated agent tasks (no shared session history).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger("crow_agent.cron")

DEFAULT_SCHEDULE_PATH = Path.home() / ".crow_agent" / "schedule.json"


_CRON_MAX_RETRIES = 3


@dataclass
class CronJob:
    """A single scheduled job."""
    id: str
    prompt: str           # the task/instruction to execute
    interval_seconds: int # how often to run
    last_run: float = 0.0 # unix timestamp of last execution
    enabled: bool = True
    model_override: str | None = None   # optional model name override
    provider_override: str | None = None  # optional provider name override
    extra: dict[str, Any] = field(default_factory=dict)
    last_result: str = ""           # most recent result text (audit trail)
    last_error: str = ""            # most recent error text (audit trail)
    consecutive_failures: int = 0   # incremented on each failure, reset on success


class CronEngine:
    """Async scheduler. Ticks every `resolution` seconds, executes due jobs."""

    def __init__(
        self,
        schedule_path: str | Path | None = None,
        resolution: float = 30.0,
    ) -> None:
        self._schedule_path = Path(schedule_path) if schedule_path else DEFAULT_SCHEDULE_PATH
        self._resolution = resolution
        self._jobs: dict[str, CronJob] = {}
        self._task: asyncio.Task | None = None
        self._runner: Callable[[CronJob], Awaitable[None]] | None = None
        self._notify_fn: Callable[[str, str, str | None], Awaitable[None]] | None = None
        self._running: set[str] = set()
        self._load()

    # --- schedule file ---

    def _load(self) -> None:
        """Load jobs from JSON schedule file."""
        if not self._schedule_path.exists():
            self._save()
            return
        try:
            data = json.loads(self._schedule_path.read_text(encoding="utf-8"))
            for entry in data.get("jobs", []):
                job = CronJob(
                    id=entry["id"],
                    prompt=entry["prompt"],
                    interval_seconds=entry["interval_seconds"],
                    last_run=entry.get("last_run", 0.0),
                    enabled=entry.get("enabled", True),
                    model_override=entry.get("model_override"),
                    provider_override=entry.get("provider_override"),
                    extra=entry.get("extra", {}),
                    last_result=entry.get("last_result", ""),
                    last_error=entry.get("last_error", ""),
                    consecutive_failures=entry.get("consecutive_failures", 0),
                )
                self._jobs[job.id] = job
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load schedule: %s", exc)

    def _save(self) -> None:
        """Persist jobs to JSON schedule file."""
        self._schedule_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "jobs": [
                {
                    "id": j.id,
                    "prompt": j.prompt,
                    "interval_seconds": j.interval_seconds,
                    "last_run": j.last_run,
                    "enabled": j.enabled,
                    "model_override": j.model_override,
                    "provider_override": j.provider_override,
                    "extra": j.extra,
                    "last_result": j.last_result,
                    "last_error": j.last_error,
                    "consecutive_failures": j.consecutive_failures,
                }
                for j in self._jobs.values()
            ]
        }
        self._schedule_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # --- job management ---

    def add_job(
        self,
        job_id: str,
        prompt: str,
        interval_seconds: int,
        enabled: bool = True,
        model_override: str | None = None,
        provider_override: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CronJob:
        """Add or update a job and persist."""
        job = CronJob(
            id=job_id,
            prompt=prompt,
            interval_seconds=interval_seconds,
            enabled=enabled,
            model_override=model_override,
            provider_override=provider_override,
            extra=extra or {},
        )
        self._jobs[job_id] = job
        self._save()
        return job

    def remove_job(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._save()

    def jobs(self) -> list[CronJob]:
        return list(self._jobs.values())

    # --- execution ---

    def set_runner(self, fn: Callable[[CronJob], Awaitable[None]]) -> None:
        """Set the async callable that executes a job."""
        self._runner = fn

    def set_notify(self, fn: Callable[[str, str, str | None], Awaitable[None]] | None) -> None:
        """Set the async callback for job result notifications.

        Callback signature: (job_id, result, error)
        error=None on success, str on failure (after retries exhausted).
        """
        self._notify_fn = fn

    async def _execute(self, job: CronJob) -> None:
        """Run a job with retry + backoff. Disables after 3 consecutive failures."""
        if self._runner is None:
            logger.warning("No runner configured for cron. Skipping job %s.", job.id)
            return
        logger.info("Cron executing job: %s", job.id)

        last_error = ""
        for attempt in range(_CRON_MAX_RETRIES):
            try:
                await self._runner(job)
                # Success
                job.last_run = time.time()
                job.consecutive_failures = 0
                job.last_error = ""
                if attempt > 0:
                    logger.info("Cron job %s recovered on attempt %d", job.id, attempt + 1)
                if self._notify_fn:
                    await self._notify_fn(job.id, job.last_result, None)
                break
            except Exception as exc:
                last_error = str(exc)
                # Detect config errors (missing model, bad key) — don't count as failure
                _cfg_err_markers = ("No model for provider", "auth failed", "401", "403")
                if any(m in last_error for m in _cfg_err_markers):
                    logger.error(
                        "Cron job %s has config error (NOT retrying): %s",
                        job.id, last_error,
                    )
                    job.last_error = last_error
                    break  # don't retry, don't disable
                job.consecutive_failures += 1
                job.last_error = last_error
                logger.warning(
                    "Cron job %s failed (attempt %d/%d): %s",
                    job.id, attempt + 1, _CRON_MAX_RETRIES, last_error,
                )
                if attempt < _CRON_MAX_RETRIES - 1:
                    await asyncio.sleep(10 * (attempt + 1))  # 10s, 20s backoff
        else:
            # All attempts exhausted — disable + notify
            job.enabled = False
            job.last_run = time.time()  # prevent re-tick loop
            logger.error(
                "Cron job %s disabled after %d consecutive failures. Last: %s",
                job.id, _CRON_MAX_RETRIES, last_error,
            )
            if self._notify_fn:
                await self._notify_fn(job.id, "", last_error)

        self._running.discard(job.id)
        self._save()

    async def _loop(self) -> None:
        """Main scheduler loop."""
        logger.info("Cron engine started. Resolution=%.1fs, Jobs=%d", self._resolution, len(self._jobs))
        while True:
            await asyncio.sleep(self._resolution)
            now = time.time()
            for job in self._jobs.values():
                if not job.enabled:
                    continue
                if now - job.last_run >= job.interval_seconds:
                    if job.id in self._running:
                        logger.debug("Skipping job %s — previous still running", job.id)
                        continue
                    self._running.add(job.id)
                    asyncio.create_task(self._execute(job))

    # --- lifecycle ---

    def start(self) -> None:
        """Start the scheduler loop as a background asyncio task."""
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @staticmethod
    def make_runner(
        provider_name: str = "opencode-go",
        model: str | None = None,
        db_path: str | None = None,
    ) -> Callable[[CronJob], Awaitable[None]]:
        """Return an async runner that executes a job via an isolated AIAgent.

        Stores result in job.last_result on success.
        Exceptions propagate to _execute for retry handling.

        Usage:
            engine = CronEngine()
            engine.set_runner(CronEngine.make_runner(provider_name="opencode_go"))
            engine.start()
        """
        async def _run(job: CronJob) -> None:
            from .run_agent import AIAgent, Trigger, TriggerSource
            from .model_tools import register_builtins
            from .toolsets import ToolRegistry

            tools = ToolRegistry()
            register_builtins(tools)

            agent = AIAgent(
                session_id=f"cron-{job.id}",
                provider_name=job.provider_override or provider_name,
                model=job.model_override or model,
                db_path=db_path,
                tool_registry=tools,
            )
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, agent.run, Trigger(source=TriggerSource.USER, prompt=job.prompt)),
                    timeout=300,
                )
                job.last_result = result[:500]
                logger.info("Cron job %s completed: %s", job.id, result[:200])
            except asyncio.TimeoutError:
                logger.warning("Cron job %s timed out after 300s", job.id)
                raise
            finally:
                agent.close()

        return _run
