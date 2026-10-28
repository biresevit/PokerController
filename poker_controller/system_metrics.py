"""Small, side-effect-free machine resource snapshot for cloud telemetry."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import psutil


def _controller_version() -> str:
    try:
        release = version("poker-controller")
    except PackageNotFoundError:
        release = "development"
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            check=True, timeout=1,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return f"{release}+{commit}" if commit else release


POKER_CONTROLLER_VERSION = _controller_version()
# Prime psutil's non-blocking counter. Subsequent samples represent CPU usage
# since the previous telemetry sample instead of an arbitrary 200 ms instant.
psutil.cpu_percent(interval=None)


def _pdecision_version() -> str | None:
    base = os.environ.get("PDECISION_URL", "http://127.0.0.1:5005").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
            value = json.load(response).get("version")
        return str(value)[:100] if value else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def sample() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(Path.cwd().anchor or "C:\\")
    return {
        "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 2),
        "memory_percent": round(float(memory.percent), 2),
        "disk_percent": round(float(disk.percent), 2),
        "disk_free_bytes": int(disk.free),
        "disk_total_bytes": int(disk.total),
        "poker_controller_version": POKER_CONTROLLER_VERSION[:100],
        "pdecision_version": _pdecision_version(),
    }
