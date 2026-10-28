"""Small, side-effect-free machine resource snapshot for cloud telemetry."""
from __future__ import annotations

from pathlib import Path
import psutil


def sample() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(Path.cwd().anchor or "C:\\")
    return {
        "cpu_percent": round(float(psutil.cpu_percent(interval=0.2)), 2),
        "memory_percent": round(float(memory.percent), 2),
        "disk_percent": round(float(disk.percent), 2),
        "disk_free_bytes": int(disk.free),
        "disk_total_bytes": int(disk.total),
    }
