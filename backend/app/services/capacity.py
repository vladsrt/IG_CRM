"""Server capacity guard.

Keeps the test box (or a small worker host) from being driven into the ground
by too many concurrent headless Chromium instances. Reads live CPU / RAM via
psutil and counts only OUR browser processes (matched by the per-launch profile
marker), not the operator's everyday Chrome.

Used by the Celery worker as a soft gate: if the box is over budget the task
self-retries with backoff instead of spawning yet another browser.
"""

from __future__ import annotations

import logging

import psutil

from app.core.config import settings

logger = logging.getLogger(__name__)

# cmdline markers that identify a browser WE launched (see browser_core.py:
# user_data_dir="fresh" -> tempfile prefix; proxy plugin folder per task).
_OUR_BROWSER_MARKERS = ("ig_crm_chrome_profile_", "runtime_proxy_plugin")


def count_our_browsers() -> int:
    """Count Chromium processes launched by this app (best-effort)."""
    n = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if any(marker in joined for marker in _OUR_BROWSER_MARKERS):
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return n


def snapshot() -> dict:
    """A point-in-time view of host load, for the admin panel."""
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_percent": vm.percent,
        "ram_used_mb": round(vm.used / 1024 / 1024),
        "ram_total_mb": round(vm.total / 1024 / 1024),
        "our_browsers": count_our_browsers(),
        "limits": {
            "cpu_percent": settings.CAPACITY_CPU_PERCENT,
            "ram_percent": settings.CAPACITY_RAM_PERCENT,
            "max_browsers": settings.CAPACITY_MAX_BROWSERS,
        },
    }


def has_free_capacity() -> tuple[bool, str]:
    """Return (ok, reason). ok=False means: don't start a new browser now.

    Fail-open: if psutil itself errors we allow the task through rather than
    block the whole fleet on a metrics glitch.
    """
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        ram = psutil.virtual_memory().percent
        browsers = count_our_browsers()
    except Exception as exc:  # pragma: no cover - metrics must never block work
        logger.warning("[capacity] probe failed, allowing task: %s", exc)
        return True, "capacity probe failed (fail-open)"

    if browsers >= settings.CAPACITY_MAX_BROWSERS:
        return False, f"browser ceiling reached ({browsers}/{settings.CAPACITY_MAX_BROWSERS})"
    if cpu >= settings.CAPACITY_CPU_PERCENT:
        return False, f"cpu busy ({cpu:.0f}% >= {settings.CAPACITY_CPU_PERCENT:.0f}%)"
    if ram >= settings.CAPACITY_RAM_PERCENT:
        return False, f"ram busy ({ram:.0f}% >= {settings.CAPACITY_RAM_PERCENT:.0f}%)"
    return True, "ok"
