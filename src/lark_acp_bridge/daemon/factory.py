"""Platform detection and daemon adapter factory."""

from __future__ import annotations

import sys

from .base import DaemonAdapter


def get_daemon_adapter() -> DaemonAdapter:
    """Return the appropriate DaemonAdapter for the current OS.

    Raises RuntimeError on unsupported platforms.
    """
    if sys.platform == "linux":
        from .systemd import SystemdAdapter
        return SystemdAdapter()
    if sys.platform == "darwin":
        from .launchd import LaunchdAdapter
        return LaunchdAdapter()
    if sys.platform == "win32":
        from .schtasks import SchtasksAdapter
        return SchtasksAdapter()
    raise RuntimeError(
        f"当前平台 {sys.platform!r} 不支持 daemon 模式。"
        "支持: Linux (systemd), macOS (launchd), Windows (schtasks)。"
    )
