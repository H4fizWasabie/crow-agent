"""Background sensing module — Crow's environmental awareness.

Runs a background thread that polls system metrics and watches directories
for file changes. Feeds into heartbeat context so Crow is never blind
between turns.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.sensors")


@dataclass
class SystemSnapshot:
    """A point-in-time capture of system metrics."""

    cpu_percent: float = 0.0
    ram_total_mb: float = 0
    ram_used_mb: float = 0
    disk_total_gb: float = 0
    disk_used_gb: float = 0
    load_average: float = 0.0
    timestamp: float = 0.0

    @classmethod
    def take(cls) -> SystemSnapshot:
        snap = cls(timestamp=time.time())
        try:
            import psutil
            snap.cpu_percent = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            snap.ram_total_mb = mem.total / (1024 * 1024)
            snap.ram_used_mb = mem.used / (1024 * 1024)
            disk = psutil.disk_usage("/")
            snap.disk_total_gb = disk.total / (1024 * 1024 * 1024)
            snap.disk_used_gb = disk.used / (1024 * 1024 * 1024)
            snap.load_average = psutil.getloadavg()[0]
        except ImportError:
            snap._fallback_take()
        except Exception as exc:
            logger.debug("System snapshot failed: %s", exc)
        return snap

    def _fallback_take(self) -> None:
        try:
            with open("/proc/loadavg") as f:
                self.load_average = float(f.read().split()[0])
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemTotal" in line:
                        self.ram_total_mb = float(line.split()[1]) / 1024
                    elif "MemAvailable" in line:
                        self.ram_used_mb = self.ram_total_mb - float(line.split()[1]) / 1024
        except Exception:
            pass
        try:
            stat = os.statvfs("/")
            self.disk_total_gb = (stat.f_frsize * stat.f_blocks) / (1024 ** 3)
            self.disk_used_gb = (stat.f_frsize * (stat.f_blocks - stat.f_bavail)) / (1024 ** 3)
        except Exception:
            pass

    def to_summary(self) -> str:
        ram_pct = (self.ram_used_mb / self.ram_total_mb * 100) if self.ram_total_mb > 0 else 0
        disk_pct = (self.disk_used_gb / self.disk_total_gb * 100) if self.disk_total_gb > 0 else 0
        return (
            f"CPU {self.cpu_percent:.0f}% | RAM {ram_pct:.0f}% ({self.ram_used_mb:.0f}/{self.ram_total_mb:.0f}MB) | "
            f"Disk {disk_pct:.0f}% ({self.disk_used_gb:.0f}/{self.disk_total_gb:.0f}GB) | "
            f"Load {self.load_average:.1f}"
        )

    def get_alerts(self) -> list[str]:
        alerts = []
        ram_pct = (self.ram_used_mb / self.ram_total_mb * 100) if self.ram_total_mb > 0 else 0
        disk_pct = (self.disk_used_gb / self.disk_total_gb * 100) if self.disk_total_gb > 0 else 0
        if disk_pct > 90:
            alerts.append(f"Disk critical: {disk_pct:.0f}% used ({self.disk_used_gb:.0f}/{self.disk_total_gb:.0f}GB)")
        elif disk_pct > 80:
            alerts.append(f"Disk warning: {disk_pct:.0f}% used")
        if ram_pct > 90:
            alerts.append(f"RAM critical: {ram_pct:.0f}% used ({self.ram_used_mb:.0f}/{self.ram_total_mb:.0f}MB)")
        elif ram_pct > 80:
            alerts.append(f"RAM warning: {ram_pct:.0f}% used")
        if self.load_average > 5:
            alerts.append(f"High load: {self.load_average:.1f}")
        return alerts


class BackgroundSensor:
    """Background thread that polls system metrics and watches directories."""

    def __init__(self, poll_interval: float = 30.0, max_snapshots: int = 60) -> None:
        self.poll_interval = poll_interval
        self.max_snapshots = max_snapshots
        self._snapshots: list[SystemSnapshot] = []
        self._file_changes: list[dict] = []
        self._watch_dirs: dict[str, dict[str, float]] = {}
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def watch_directory(self, directory: str) -> None:
        with self._lock:
            self._watch_dirs[directory] = {}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("BackgroundSensor started (poll=%ds)", self.poll_interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)

    def get_recent_snapshots(self, n: int = 10) -> list[SystemSnapshot]:
        with self._lock:
            return self._snapshots[-n:]

    def get_context_delta(self) -> dict[str, Any]:
        with self._lock:
            snapshots = self._snapshots[-5:]
            alerts = []
            for s in snapshots:
                alerts.extend(s.get_alerts())
            unique_alerts = list(dict.fromkeys(alerts))[-3:]
            changes = self._file_changes[-10:]
            return {
                "snapshots": [s.to_summary() for s in snapshots],
                "alerts": unique_alerts,
                "file_changes": [c.get("summary", "") for c in changes],
            }

    def _loop(self) -> None:
        while self._running:
            try:
                snap = SystemSnapshot.take()
                with self._lock:
                    self._snapshots.append(snap)
                    if len(self._snapshots) > self.max_snapshots:
                        self._snapshots = self._snapshots[-self.max_snapshots:]
                    self._scan_watched_dirs()
                for alert in snap.get_alerts():
                    logger.warning("Sensor alert: %s", alert)
            except Exception as exc:
                logger.debug("Sensor poll error: %s", exc)
            time.sleep(self.poll_interval)

    def _scan_watched_dirs(self) -> None:
        for directory, prev_state in list(self._watch_dirs.items()):
            try:
                current_state: dict[str, float] = {}
                for entry in os.scandir(directory):
                    if entry.is_file():
                        mtime = entry.stat().st_mtime
                        current_state[entry.name] = mtime
                        if entry.name not in prev_state:
                            self._file_changes.append({"path": entry.path, "summary": f"Created: {entry.name}"})
                        elif mtime != prev_state[entry.name]:
                            self._file_changes.append({"path": entry.path, "summary": f"Modified: {entry.name}"})
                for old_name in prev_state:
                    if old_name not in current_state:
                        self._file_changes.append({"path": "", "summary": f"Deleted: {old_name}"})
                self._watch_dirs[directory] = current_state
                if len(self._file_changes) > 100:
                    self._file_changes = self._file_changes[-100:]
            except Exception as exc:
                logger.debug("Dir scan failed for %s: %s", directory, exc)


_sensor_instance: BackgroundSensor | None = None


def get_sensor() -> BackgroundSensor | None:
    return _sensor_instance


def init_sensor(poll_interval: float = 30.0) -> BackgroundSensor:
    global _sensor_instance
    if _sensor_instance is None:
        _sensor_instance = BackgroundSensor(poll_interval=poll_interval)
    return _sensor_instance
